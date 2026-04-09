"""
SVG Method Drawing pipeline: load markdown → method proposer → image generation →
SAM3 segmentation → icon extraction → SVG generation → validation →
icon replacement → SVG render → advocate review →
[accept → finalize | refine → validation → ... | regenerate → image generation → ...]

Reuses the llm module for LLM calls and the SAM3 service for segmentation.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import base64
import time as _time
from pathlib import Path

from PIL import Image, ImageDraw
from langgraph.graph import StateGraph, END

# Shared definitions formerly in graphs.method_drawing_pipeline (deleted).
# Inlined here to avoid circular imports and keep the SVG pipeline self-contained.
from graphs._method_shared import (
    MethodDrawingPipelineState,
    load_markdown_node,
    method_data_explorer_node,
    method_proposer_node,
    QUALITY_THRESHOLDS,
    SCIENTIFIC_DIAGRAM_GUIDELINES,
    _extract_drawing_instruction,
)
from graphs.figure_pipeline import (
    _ensure_gpt as ensure_gpt,
    _load_style_few_shots,
    _build_style_few_shot_messages,
)
from graphs.svg_utils import extract_json_block, _parse_review_json, _merge_issues

# architect_review_node + advocate_review_node now defined locally
from graphs.svg_utils import (
    merge_overlapping_boxes,
    extract_svg_code,
    validate_svg_syntax,
    get_svg_dimensions,
    calculate_scale_factors,
    svg_to_png,
    count_base64_images,
    validate_base64_images,
    draw_samed_image,
    draw_sam_overlay,
    draw_ocr_overlay,
    build_composite_image,
    validate_text_boundaries,
    run_post_render_checks,
    build_automated_refinement_instructions,
    load_pipeline_config,
)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
REPO_ROOT = PROMPT_DIR.parent
VISUALIZATION_EXAMPLES_DIR = REPO_ROOT / "configs" / "visualization"

logger = logging.getLogger(__name__)


def _get_sam_config() -> dict:
    """Return the sam section from pipeline config with defaults."""
    cfg = load_pipeline_config().get("sam", {})
    return cfg


def _get_icon_prompts() -> set:
    """Return the set of SAM prompts that represent extractable icons."""
    cfg = _get_sam_config()
    return set(cfg.get("icon_prompts", ["icon", "robot", "animal", "person", "logo", "subfigure", "graph"]))


def _get_valid_classes() -> tuple[list[str], list[str]]:
    """Return (structural, complex) valid class lists from config."""
    cfg = _get_sam_config()
    vc = cfg.get("valid_classes", {})
    structural = vc.get(
        "structural",
        [
            "rectangle",
            "rounded_rectangle",
            "arrow",
            "circle",
            "diamond",
            "text_block",
            "dashed_rectangle",
            "dotted_rectangle",
            "dash_dot_rectangle",
            "dashed_rounded_rectangle",
            "dotted_rounded_rectangle",
            "dash_dot_rounded_rectangle",
            "bracket",
            "line",
            "dashed_line",
            "dotted_line",
            "stack",
            "cube",
            "cuboid",
            "grid",
            "document",
            "hexagon",
            "triangle",
            "star",
        ],
    )
    complex_ = vc.get(
        "complex",
        [
            "icon",
            "graph",
            "subfigure",
            "logo",
            "robot",
            "animal",
            "person",
        ],
    )
    return structural, complex_


def _normalize_class_name(name: str) -> str:
    """Normalize class/prompt names: 'dashed rectangle' → 'dashed_rectangle'."""
    return name.strip().replace(" ", "_").replace("-", "_").lower()


from graphs.svg_utils import load_prompt as _load_prompt  # noqa: E402 — unified prompt loader


def _save_node_prompt(run_dir: str, node_name: str, system_prompt: str, user_prompt: str, suffix: str = "") -> None:
    """Save system + user prompts for a node to disk for debugging."""
    if not run_dir:
        return
    tag = f"_{suffix}" if suffix else ""
    prompt_path = Path(run_dir) / f"prompt_{node_name}{tag}.md"
    parts = []
    if system_prompt:
        parts.append(f"# System Prompt\n\n{system_prompt}")
    if user_prompt:
        parts.append(f"# User Prompt\n\n{user_prompt}")
    prompt_path.write_text("\n\n---\n\n".join(parts), encoding="utf-8")


def _dedup_boxes_by_iou(
    boxes: list,
    iou_threshold: float = 0.7,
    verbose: bool = False,
    image_path: str | None = None,
) -> list:
    """Greedy NMS deduplication: sort by confidence, suppress lower-score overlaps."""
    if not boxes:
        return boxes

    def _iou(a: dict, b: dict) -> float:
        x1 = max(a["x1"], b["x1"])
        y1 = max(a["y1"], b["y1"])
        x2 = min(a["x2"], b["x2"])
        y2 = min(a["y2"], b["y2"])
        inter = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = (a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
        area_b = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    # Sort by confidence descending — highest-confidence boxes survive
    sorted_boxes = sorted(boxes, key=lambda b: b.get("score", 0), reverse=True)
    keep = []
    removed_labels = []
    removed_pairs = []

    for box in sorted_boxes:
        suppressed = False
        for kept in keep:
            iou_value = _iou(box, kept)
            if iou_value > iou_threshold:
                removed_labels.append(box.get("label", "?"))
                removed_pairs.append(
                    {
                        "kept": dict(kept),
                        "removed": dict(box),
                        "iou": iou_value,
                    }
                )
                suppressed = True
                break
        if not suppressed:
            keep.append(box)

    if removed_pairs and image_path and Path(image_path).exists():
        try:
            with Image.open(image_path) as _img:
                base = _img.convert("RGBA")
            overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay, "RGBA")

            for pair in removed_pairs:
                kept_box = pair["kept"]
                removed_box = pair["removed"]
                iou_value = pair["iou"]
                draw.rectangle(
                    [kept_box["x1"], kept_box["y1"], kept_box["x2"], kept_box["y2"]],
                    outline=(0, 170, 0, 255),
                    fill=(0, 200, 0, 72),
                    width=4,
                )
                draw.rectangle(
                    [removed_box["x1"], removed_box["y1"], removed_box["x2"], removed_box["y2"]],
                    outline=(220, 0, 0, 255),
                    fill=(255, 0, 0, 72),
                    width=4,
                )
                text_x = min(kept_box["x1"], removed_box["x1"])
                text_y = max(0, min(kept_box["y1"], removed_box["y1"]) - 18)
                label_text = (
                    f"keep {kept_box.get('label', '?')} / drop {removed_box.get('label', '?')} IoU={iou_value:.2f}"
                )
                draw.rectangle(
                    [text_x, text_y, min(base.width, text_x + 260), text_y + 16],
                    fill=(255, 255, 255, 200),
                )
                draw.text((text_x + 2, text_y + 1), label_text, fill=(0, 0, 0, 255))

            out_path = Path(image_path).resolve().parent / "iou_dedup_overlay.png"
            Image.alpha_composite(base, overlay).convert("RGB").save(out_path)
            base.close()
            overlay.close()
            if verbose:
                logger.debug("  IoU dedup overlay saved → %s", out_path.name)
        except Exception as e:
            if verbose:
                logger.warning("  Failed to save IoU dedup overlay (%s)", e)

    if verbose and removed_labels:
        logger.debug("  IoU dedup: removed %d duplicate(s): %s", len(removed_labels), removed_labels)

    return keep


def _build_box_context(valid_boxes: list, icon_infos: list) -> str:
    """Build structured text describing each AF box with its type, coordinates, and contained texts.

    Uses the is_icon field set by LLM classification. Falls back to checking
    icon_infos labels if is_icon is absent.
    """
    icon_labels = {info.get("label_clean", "") for info in (icon_infos or [])}
    lines = ["## Detected Regions (AF boxes)", ""]
    for box in valid_boxes:
        label = box.get("label", "")
        label_clean = label.replace("<", "").replace(">", "")
        # Prefer LLM-corrected class; fall back to original SAM prompt
        prompt = box.get("corrected_class", box.get("prompt", "unknown"))
        # Use LLM-classified is_icon if available, fall back to icon_infos membership
        is_icon = box.get("is_icon", label_clean in icon_labels)
        desc = box.get("description", "")
        desc_note = f" ({desc})" if desc else ""
        kind = (
            "ICON (keep as gray placeholder — will be replaced with image later)"
            if is_icon
            else f"STRUCTURAL/{prompt} (replicate with SVG code — rect, path, line, text, etc.)"
        )
        coords = f"({box['x1']},{box['y1']})-({box['x2']},{box['y2']})"
        w, h = box["x2"] - box["x1"], box["y2"] - box["y1"]
        lines.append(f"- {label} [{kind}] at {coords} ({w}x{h}){desc_note}")
        # List contained texts with their own bboxes and font size estimates
        for t in box.get("texts", []):
            fs = t.get("font_size_est")
            fs_note = f" font_size~{fs}px" if fs is not None else ""
            tc = f"({t['x1']},{t['y1']})-({t['x2']},{t['y2']})" if "x1" in t else ""
            lines.append(f'    text: "{t["text"]}"{fs_note} at {tc}')
    return "\n".join(lines)


def _build_ocr_context(ocr_texts: list) -> str:
    """Build structured OCR text context for the LLM.

    Each entry includes bbox, estimated font size, rotation, and which AF box(es) contain it.
    """
    if not ocr_texts:
        return ""
    lines = [
        "## OCR-Detected Text",
        "Coordinates are absolute pixels matching the SVG viewBox. "
        "`font_size` is estimated from the SHORT axis of the bbox "
        "(correctly handles both horizontal and vertical text). "
        "`rotation` indicates text orientation (-90 = vertical/bottom-to-top). "
        'For rotated text, use `transform="rotate(-90 cx cy)"` in SVG. '
        "`in_boxes` lists which AF region(s) contain this text — "
        "ensure those SVG boxes visually contain their labeled texts without clipping.",
        "",
    ]
    for ocr in ocr_texts:
        text = ocr.get("text", "")
        if not text:
            continue
        conf = ocr.get("confidence", 0)
        if "x1" in ocr:
            fs = ocr.get("font_size_est")
            fs_note = f" font_size~{fs}px" if fs is not None else ""
            rot = ocr.get("rotation", 0)
            rot_note = f" rotation={rot}" if rot != 0 else ""
            boxes = ocr.get("boxes", [])
            box_note = f" in_boxes={boxes}" if boxes else ""
            lines.append(
                f'- "{text}" at ({ocr["x1"]},{ocr["y1"]})-({ocr["x2"]},{ocr["y2"]})'
                f"{fs_note}{rot_note}{box_note} conf={conf:.2f}"
            )
        else:
            lines.append(f'- "{text}" conf={conf:.2f}')
    return "\n".join(lines)


def _diff_svg_boxes(svg_code: str, valid_boxes: list) -> list:
    """Compare AF placeholder <g> boxes in SVG against original SAM boxes.

    Returns a list of dicts — one per SAM icon box — with position deltas.
    """
    # Build label → SAM box map (only icon boxes have AF placeholders)
    label_to_sam = {b["label"].replace("<", "").replace(">", ""): b for b in valid_boxes if b.get("is_icon")}

    # Extract AF groups: <g id="AF01"> ... <rect x y width height> ... </g>
    svg_af_ids: set = set(
        re.findall(
            r'<g\b[^>]*\bid=["\']?(AF\d+)["\']?',
            svg_code,
            re.IGNORECASE,
        )
    )

    diffs = []
    for g_m in re.finditer(
        r'<g\b[^>]*\bid=["\']?(AF\d+)["\']?[^>]*>(.*?)</g>',
        svg_code,
        re.DOTALL | re.IGNORECASE,
    ):
        af_id = g_m.group(1)
        g_content = g_m.group(2)

        rect_m = re.search(r"<rect\b([^/]*?)(?:/>|>)", g_content, re.DOTALL | re.IGNORECASE)
        if not rect_m:
            diffs.append({"label": af_id, "issue": "no_rect"})
            continue

        attrs = rect_m.group(1)

        def _attr(name: str) -> float | None:
            m = re.search(rf'\b{name}=["\']?([0-9.\-]+)["\']?', attrs)
            return float(m.group(1)) if m else None

        x, y, w, h = _attr("x"), _attr("y"), _attr("width"), _attr("height")
        if any(v is None for v in (x, y, w, h)):
            diffs.append({"label": af_id, "issue": "incomplete_rect"})
            continue

        svg_x1, svg_y1, svg_x2, svg_y2 = x, y, x + w, y + h  # type: ignore[operator]
        sam = label_to_sam.get(af_id)
        if sam is None:
            diffs.append({"label": af_id, "issue": "no_sam_match"})
            continue

        dx1 = svg_x1 - sam["x1"]
        dy1 = svg_y1 - sam["y1"]
        dx2 = svg_x2 - sam["x2"]
        dy2 = svg_y2 - sam["y2"]
        diffs.append(
            {
                "label": af_id,
                "svg": (int(svg_x1), int(svg_y1), int(svg_x2), int(svg_y2)),
                "sam": (sam["x1"], sam["y1"], sam["x2"], sam["y2"]),
                "delta": (int(dx1), int(dy1), int(dx2), int(dy2)),
                "max_err": int(max(abs(dx1), abs(dy1), abs(dx2), abs(dy2))),
            }
        )

    # SAM icon boxes not found in SVG at all
    for lab in label_to_sam:
        if lab not in svg_af_ids:
            diffs.append({"label": lab, "issue": "missing_from_svg"})

    return diffs


def _diff_ocr_texts(png_path: str, original_ocr: list) -> dict:
    """Run OCR on a rendered SVG PNG and diff against original detected texts.

    Returns matched / missing / extra text sets, or {"error": ...} on failure.
    """
    from services.ocr.client import OcrServiceClient

    ocr_url = os.environ.get("OCR_SERVICE_URL", "http://127.0.0.1:8002")
    client = OcrServiceClient(ocr_url)
    try:
        result = client.predict(image_path=str(Path(png_path).resolve()))
    except Exception as e:
        return {"error": str(e)}

    svg_texts: set = set()
    for det in result.get("items", []):
        t = det.get("text", "").strip()
        ocr_conf = load_pipeline_config().get("ocr", {}).get("confidence_threshold", 0.6)
        if t and float(det.get("score", 0)) >= ocr_conf:
            svg_texts.add(t)

    orig_texts = {ocr.get("text", "").strip() for ocr in original_ocr if ocr.get("text", "").strip()}
    return {
        "matched": sorted(orig_texts & svg_texts),
        "missing": sorted(orig_texts - svg_texts),
        "extra": sorted(svg_texts - orig_texts),
    }


def _print_svg_diff(svg_code: str, png_path: str | None, valid_boxes: list, original_ocr: list, tag: str) -> None:
    """Print box and OCR diff to stdout (verbose-only helper)."""
    # Box diff
    box_diffs = _diff_svg_boxes(svg_code, valid_boxes)
    if box_diffs:
        logger.debug("  [%s] AF box diff (SVG vs SAM):", tag)
        for d in box_diffs:
            lab = d["label"]
            if "issue" in d:
                logger.debug("    %s: %s", lab, d["issue"].upper())
            else:
                ok = "✓" if d["max_err"] <= 5 else ("~" if d["max_err"] <= 20 else "✗")
                logger.debug(
                    "    %s: SVG=%s SAM=%s Δ=%s max=%spx %s", lab, d["svg"], d["sam"], d["delta"], d["max_err"], ok
                )
    else:
        logger.debug("  [%s] No AF icon boxes to compare", tag)

    # OCR text diff
    if png_path and Path(png_path).exists():
        ocr_diff = _diff_ocr_texts(png_path, original_ocr)
        if "error" in ocr_diff:
            logger.debug("  [%s] OCR diff failed: %s", tag, ocr_diff["error"])
        else:
            n_match = len(ocr_diff["matched"])
            n_miss = len(ocr_diff["missing"])
            n_extra = len(ocr_diff["extra"])
            logger.debug("  [%s] OCR diff — matched=%d, missing=%d, extra=%d", tag, n_match, n_miss, n_extra)
            if ocr_diff["missing"]:
                logger.debug("    Missing: %s", ocr_diff["missing"][:10])
            if ocr_diff["extra"]:
                logger.debug("    Extra:   %s", ocr_diff["extra"][:10])


def _strip_base64_from_svg(svg_code: str) -> str:
    """Replace base64 image data in SVG with short placeholders.

    This keeps the SVG structure intact (id, x, y, width, height) but removes
    the massive base64 payloads so the LLM can focus on layout, not data.
    """

    def _replacer(m: re.Match) -> str:
        return m.group(1) + "BASE64_DATA_STRIPPED" + m.group(2)

    return re.sub(
        r'(href=["\']data:image/[^;]+;base64,)[A-Za-z0-9+/=]+(["\'])',
        _replacer,
        svg_code,
    )


def _restore_base64_in_svg(new_svg: str, original_svg: str) -> str:
    """Restore base64 data from original_svg into new_svg where stripped.

    Matches <image> tags by id attribute and restores the full href.
    Also handles cases where the LLM kept the literal 'BASE64_DATA_STRIPPED' text.
    """
    # Build map: image id → full href from original
    id_to_href: dict[str, str] = {}
    for m in re.finditer(
        r'<image[^>]*\bid=["\']([^"\']+)["\'][^>]*href=["\']([^"\']+)["\']',
        original_svg,
        re.IGNORECASE,
    ):
        id_to_href[m.group(1)] = m.group(2)
    # Also match href before id
    for m in re.finditer(
        r'<image[^>]*href=["\']([^"\']+)["\'][^>]*\bid=["\']([^"\']+)["\']',
        original_svg,
        re.IGNORECASE,
    ):
        id_to_href[m.group(2)] = m.group(1)

    if not id_to_href:
        return new_svg

    result = new_svg
    for img_id, full_href in id_to_href.items():
        # Find image tags with this id that have stripped data
        pattern = (
            rf'(<image[^>]*\bid=["\']?{re.escape(img_id)}["\']?[^>]*href=["\'])'
            r"data:image/[^;]+;base64,BASE64_DATA_STRIPPED"
            r'(["\'])'
        )
        result = re.sub(pattern, lambda m, h=full_href: m.group(1) + h + m.group(2), result, flags=re.IGNORECASE)

        # Also handle href before id
        pattern2 = (
            r'(<image[^>]*href=["\'])'
            r"data:image/[^;]+;base64,BASE64_DATA_STRIPPED"
            rf'(["\'][^>]*\bid=["\']?{re.escape(img_id)}["\']?)'
        )
        result = re.sub(pattern2, lambda m, h=full_href: m.group(1) + h + m.group(2), result, flags=re.IGNORECASE)

    return result


# ── State ─────────────────────────────────────────────────────────────


class SVGMethodPipelineState(MethodDrawingPipelineState, total=False):
    """State for the SVG method drawing pipeline."""

    # Image generation
    generated_image_path: str  # Gemini raster output (figure.png)
    skip_image_generation: bool  # Use existing figure.png for testing
    reuse_image_dir: str  # Copy figure.png from this dir instead of generating

    # SAM3 segmentation
    samed_image_path: str  # Annotated image with gray boxes
    boxlib_path: str  # JSON with box coordinates
    valid_boxes: list  # List of detected box dicts

    # SAM3 params
    sam_prompts: str  # Comma-separated SAM3 text prompts
    sam_min_score: float  # Min confidence threshold
    sam_merge_threshold: float  # Box overlap merge threshold

    # Two-stage SAM detection
    sam_stage1_prompts: list  # Supported prompts used for stage 1
    sam_stage2_prompts: list  # Additional prompts from review stage
    sam_stage1_results: list  # Raw detections from stage 1
    sam_stage2_results: list  # Raw detections from stage 2
    sam_stage1_overlay_path: str  # Labeled overlay for review
    sam_agent_classified: bool  # True when agent pre-classified boxes (OpenCode mode)

    # OCR text detection
    ocr_texts: list  # Per-box OCR text results

    # Icon extraction
    icon_infos: list  # Per-icon metadata dicts
    visualization_icons: list  # Subset of icon_infos routed to codegen
    codegen_icon_paths: dict  # {label_clean: generated_png_path}

    # SVG generation
    template_svg_path: str  # Initial template SVG
    optimized_svg_path: str  # After LLM optimization
    final_svg_path: str  # After icon replacement
    svg_code: str  # Current SVG code in memory

    # SVG validation
    svg_valid: bool
    svg_errors: list
    svg_fix_iteration: int
    max_svg_fix_iterations: int  # default: 3

    # Coordinate alignment
    scale_factors: tuple  # (scale_x, scale_y)

    # SVG optimization
    optimize_iterations: int  # LLM optimization iterations (0 = skip)

    # Agent team review
    architecture_review_feedback: dict
    architecture_review_iteration: int
    architect_feedback: dict
    advocate_feedback: dict
    combined_score: float
    combined_issues: list
    review_history: list

    # Routing
    refinement_action: str  # "accept" | "refine" | "regenerate"
    team_iteration: int
    max_team_iterations: int  # default: 3
    # Refinement
    refined_prompt: str  # Enhanced prompt for regeneration


# ── Nodes ─────────────────────────────────────────────────────────────


def image_generation_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Generate a method architecture raster image using Gemini.

    If ``skip_image_generation`` is set and a figure.png exists in run_dir,
    reuses it instead of calling the API.
    """
    run_dir = state.get("run_dir", "")
    if not run_dir:
        return {"error": "No run_dir available", "success": False}

    run_path = Path(run_dir)
    iteration = state.get("team_iteration", 0)

    # Reuse image from a previous run directory if specified
    reuse_dir = state.get("reuse_image_dir", "")
    if reuse_dir and iteration == 0:
        reuse_path = Path(reuse_dir)
        src_figure = reuse_path / "figure.png"
        if src_figure.exists():
            dst_figure = run_path / "figure.png"
            shutil.copy2(src_figure, dst_figure)
            if state.get("verbose"):
                logger.info("Image generation SKIPPED — reused from: %s", src_figure)
            return {"generated_image_path": str(dst_figure)}
        else:
            if state.get("verbose"):
                logger.warning("reuse_image_dir set but %s not found, generating new image", src_figure)

    # Skip path: reuse existing image in current run_dir
    skip = state.get("skip_image_generation", False)
    if skip and iteration == 0:
        existing = run_path / "figure.png"
        if existing.exists():
            if state.get("verbose"):
                logger.info("Image generation SKIPPED — using existing: %s", existing)
            return {"generated_image_path": str(existing)}

    ensure_gpt()
    from llm import run_image_prompt, encode_image_to_data_url, get_backend, get_model_display

    method_description = state.get("method_description", "")
    if not method_description:
        return {"error": "No method description available", "success": False}

    drawing_instruction = _extract_drawing_instruction(method_description)

    refined_prompt = state.get("refined_prompt", "")
    if refined_prompt:
        drawing_prompt = refined_prompt
    else:
        drawing_prompt = f"{SCIENTIFIC_DIAGRAM_GUIDELINES}\n\nARCHITECTURE DIAGRAM REQUEST:\n\n{drawing_instruction}"

    few_shots = state.get("architecture_few_shots") or []
    reference_images = []
    for shot in few_shots:
        img_path = shot.get("image_path", "")
        if img_path and Path(img_path).exists():
            try:
                reference_images.append(encode_image_to_data_url(img_path))
            except (OSError, ValueError):
                pass  # Skip unreadable or invalid reference images

    if state.get("verbose"):
        logger.info(
            "Generating image (iteration %d, backend=%s, model=%s)...",
            iteration,
            get_backend(),
            get_model_display("drawing"),
        )

    try:
        image_data = run_image_prompt(
            drawing_prompt,
            reference_images=reference_images if iteration == 0 else None,
        )
    except Exception as e:
        return {"error": f"Image generation failed: {e}", "success": False}

    if not image_data:
        return {"error": "Image generation returned no data", "success": False}

    # Save with iteration suffix to preserve history; also write latest as figure.png
    iter_path = run_path / f"figure_v{iteration}.png"
    iter_path.write_bytes(image_data)
    canonical_path = run_path / "figure.png"
    shutil.copy2(iter_path, canonical_path)

    if state.get("verbose"):
        logger.info("  Saved: %s (method figure.png)", iter_path)

    return {
        "generated_image_path": str(canonical_path),
        "drawing_prompt": drawing_prompt,
    }


def _run_sam3_prompts(
    image_path: str,
    prompt_list: list[str],
    min_score: float = 0.0,
    verbose: bool = False,
) -> list[dict]:
    """Call SAM3 service with batched prompts and return raw detections.

    Sends all prompts in a single request (SAM3 server supports prompt lists).
    Returns list of dicts: {x1, y1, x2, y2, score, prompt}.
    Raises RuntimeError if the SAM3 service is unreachable.
    """
    from services.sam3.client import Sam3ServiceClient

    sam3_url = os.environ.get("SAM3_SERVICE_URL", "http://127.0.0.1:8001")
    client = Sam3ServiceClient(sam3_url)
    abs_image_path = str(Path(image_path).resolve())

    all_detected = []
    failed_count = 0

    # Batch all prompts in a single request for efficiency
    if verbose:
        logger.info("  Sending %d prompts to SAM3 in one batch...", len(prompt_list))
    try:
        result = client.predict(
            image_path=abs_image_path,
            prompts=prompt_list,
            score_threshold=min_score,
        )
        detections = result.get("results", [])
        for det in detections:
            score_val = float(det.get("score", 0))
            if score_val >= min_score:
                bbox = det["bbox"]
                prompt = det.get("prompt", "unknown")
                all_detected.append(
                    {
                        "x1": int(bbox[0]),
                        "y1": int(bbox[1]),
                        "x2": int(bbox[2]),
                        "y2": int(bbox[3]),
                        "score": score_val,
                        "prompt": prompt,
                    }
                )
        if verbose:
            # Summarize by prompt
            prompt_counts = {}
            for d in all_detected:
                p = d["prompt"]
                prompt_counts[p] = prompt_counts.get(p, 0) + 1
            for p, c in prompt_counts.items():
                logger.debug("    '%s': %d objects", p, c)
    except Exception as e:
        # Batch failed — fall back to per-prompt calls
        if verbose:
            logger.warning("  Batch request failed (%s), falling back to per-prompt calls...", e)
        for prompt in prompt_list:
            if verbose:
                logger.debug("  Detecting: '%s'", prompt)
            try:
                result = client.predict(image_path=abs_image_path, prompts=[prompt], score_threshold=min_score)
                detections = result.get("results", [])
                count = 0
                for det in detections:
                    score_val = float(det.get("score", 0))
                    if score_val >= min_score:
                        bbox = det["bbox"]
                        all_detected.append(
                            {
                                "x1": int(bbox[0]),
                                "y1": int(bbox[1]),
                                "x2": int(bbox[2]),
                                "y2": int(bbox[3]),
                                "score": score_val,
                                "prompt": prompt,
                            }
                        )
                        count += 1
                if verbose:
                    logger.debug("    '%s': %d objects", prompt, count)
            except Exception as prompt_err:
                failed_count += 1
                if verbose:
                    logger.error("    '%s' failed: %s", prompt, prompt_err)

        # If ALL prompts failed, the service is likely down
        if failed_count == len(prompt_list):
            raise RuntimeError(
                f"SAM3 service unreachable: all {len(prompt_list)} prompts failed. Check SAM3 at {sam3_url}"
            )

    if verbose:
        msg = "  Total: %d objects from %d prompts"
        args = [len(all_detected), len(prompt_list)]
        if failed_count:
            msg += " (%d prompts failed)"
            args.append(failed_count)
        logger.info(msg, *args)
    return all_detected


def _get_sam_supported_prompts() -> list[str]:
    """Return the full list of SAM3-supported prompt types.

    Used as direct SAM input in stage 1 (run all prompts).
    Also exposed to OpenCode agents as vocabulary reference.
    """
    cfg = _get_sam_config()
    return cfg.get(
        "supported_prompts",
        [
            "rectangle",
            "rounded rectangle",
            "arrow",
            "circle",
            "diamond",
            "icon",
            "text block",
            "dashed rectangle",
            "dotted rectangle",
            "dash-dot rectangle",
            "dashed rounded rectangle",
            "dotted rounded rectangle",
            "dash-dot rounded rectangle",
            "bracket",
            "line",
            "stack",
            "cube",
            "cuboid",
            "grid",
            "document",
            "hexagon",
            "triangle",
            "star",
        ],
    )


def _boxes_from_detections(detections: list[dict], start_id: int = 0) -> list[dict]:
    """Convert raw detection dicts into labeled box dicts."""
    boxes = []
    for i, det in enumerate(detections):
        boxes.append(
            {
                "id": start_id + i,
                "label": f"<AF>{start_id + i + 1:02d}",
                "x1": det["x1"],
                "y1": det["y1"],
                "x2": det["x2"],
                "y2": det["y2"],
                "score": det["score"],
                "prompt": det["prompt"],
            }
        )
    return boxes


# ---------------------------------------------------------------------------
# Two-stage SAM detection nodes
# ---------------------------------------------------------------------------


def sam3_detect_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Stage 1: Run ALL supported SAM prompts on figure.png. No LLM calls.

    Purely mechanical — runs every prompt from the supported vocabulary through SAM3.
    Produces sam_stage1_results.json and a labeled overlay for review.
    """
    image_path = state.get("generated_image_path", "")
    if not image_path or not Path(image_path).exists():
        return {"error": "No generated image for SAM3 segmentation", "success": False}

    run_dir = state.get("run_dir", "")
    run_path = Path(run_dir)
    verbose = state.get("verbose", False)

    sam_cfg = _get_sam_config()
    min_score = state.get("sam_min_score", sam_cfg.get("min_score", 0.0))

    # Use all supported prompts — comprehensive detection, no LLM needed
    prompt_list = _get_sam_supported_prompts()

    if verbose:
        logger.info("SAM3 Stage 1: Running %d supported prompts (no LLM)...", len(prompt_list))

    # --- Run SAM3 with all supported prompts ---
    try:
        stage1_detections = _run_sam3_prompts(image_path, prompt_list, min_score, verbose)
    except RuntimeError as e:
        return {"error": str(e), "success": False}

    # Save raw results
    with Image.open(image_path) as _img:
        img_size = _img.size
    raw_data = {
        "image_size": {"width": img_size[0], "height": img_size[1]},
        "results": stage1_detections,
    }
    raw_path = run_path / "sam_stage1_results.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, indent=2, ensure_ascii=False)

    # Build labeled boxes and draw overlay for review
    stage1_boxes = _boxes_from_detections(stage1_detections)
    with Image.open(image_path) as _img:
        image = _img.copy()
    overlay_path = str(run_path / "sam_stage1_overlay.png")
    draw_sam_overlay(image, stage1_boxes, overlay_path)

    if verbose:
        logger.info("  Stage 1: %d detections, overlay saved → sam_stage1_overlay.png", len(stage1_detections))

    return {
        "sam_stage1_prompts": prompt_list,
        "sam_stage1_results": stage1_detections,
        "sam_stage1_overlay_path": overlay_path,
    }


def sam3_review_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Stage 2: Review stage-1 overlay, run SAM3 with additional prompts.

    In OpenCode mode: agent pre-sets `sam_stage2_prompts` in state → node just runs SAM3.
    In Legacy mode: node calls LLM to review overlay and generate additional prompts.
    """
    image_path = state.get("generated_image_path", "")
    run_dir = state.get("run_dir", "")
    run_path = Path(run_dir)
    verbose = state.get("verbose", False)

    sam_cfg = _get_sam_config()
    min_score = state.get("sam_min_score", sam_cfg.get("min_score", 0.0))

    stage1_prompts = state.get("sam_stage1_prompts", [])
    stage1_results = state.get("sam_stage1_results", [])
    overlay_path = state.get("sam_stage1_overlay_path", "")

    # --- Check if agent already provided stage 2 prompts (OpenCode mode) ---
    # Use explicit flag OR non-empty pre-set prompts as the signal
    agent_classified = state.get("sam_agent_classified", False)
    stage2_prompts = state.get("sam_stage2_prompts", [])
    if agent_classified and stage2_prompts:
        if verbose:
            logger.info("SAM3 Stage 2: Using %d agent-provided prompts: %s", len(stage2_prompts), stage2_prompts)
    elif agent_classified and not stage2_prompts:
        # Agent explicitly said no additional prompts needed
        if verbose:
            logger.info("SAM3 Stage 2: Agent indicated no additional prompts needed")
        return {"sam_stage2_prompts": [], "sam_stage2_results": []}
    else:
        # --- Legacy mode: LLM reviews overlay and generates additional prompts ---
        if not overlay_path or not Path(overlay_path).exists():
            if verbose:
                logger.info("SAM3 Stage 2: No stage-1 overlay found, skipping review")
            return {"sam_stage2_prompts": [], "sam_stage2_results": []}

        supported_prompts = _get_sam_supported_prompts()
        method_desc = state.get("method_description", "")

        ensure_gpt()
        from llm import run_prompt, encode_image_to_data_url

        overlay_url = encode_image_to_data_url(overlay_path)

        # Summarize stage 1 detections by prompt
        prompt_counts = {}
        for det in stage1_results:
            p = det.get("prompt", "?")
            prompt_counts[p] = prompt_counts.get(p, 0) + 1
        stage1_summary = ", ".join(f"{p}: {c}" for p, c in prompt_counts.items())

        review_prompt = (
            "You are reviewing the first stage of object detection on a scientific diagram.\n\n"
            "## What Was Detected (Stage 1)\n"
            f"Prompts used: {stage1_prompts}\n"
            f"Detection counts: {stage1_summary}\n"
            f"Total: {len(stage1_results)} objects detected\n\n"
            "The attached image shows the original diagram with ALL stage-1 detections "
            "overlaid as colored labeled bounding boxes.\n\n"
            "## Method Description\n"
            f"{method_desc[:2000]}\n\n"
            "## Your Task\n"
            "Carefully examine the image and identify any objects, icons, shapes, connectors, "
            "or regions that are NOT covered by existing bounding boxes. Common misses include:\n"
            "- 3D shapes (cubes, cuboids, cylinders) detected as flat rectangles or missed entirely\n"
            "- Grouping elements (dashed/dotted outlines, brackets)\n"
            "- Small icons or symbols\n"
            "- Specific diagram elements (document icons, database cylinders, cloud shapes)\n"
            "- Connectors or arrows that were missed\n"
            "- Stacked/layered elements\n\n"
            "Generate ADDITIONAL SAM prompts to capture the missing elements. Use synonyms "
            "freely — the merge step will handle overlapping detections from similar prompts "
            "(e.g., use both 'cube' and '3D box' to maximize recall).\n\n"
            "## SAM3 Supported Prompts\n"
            f"{', '.join(supported_prompts)}\n"
            "You may also use free-form names.\n\n"
            "## Output Format\n"
            "Output ONLY a JSON array of additional prompt strings. "
            "If coverage is already complete, output an empty array [].\n"
            '```json\n["cube", "dashed_rectangle", ...]\n```'
        )

        if verbose:
            _save_node_prompt(run_dir, "sam3_review", "", review_prompt)
            logger.info("SAM3 Stage 2: Reviewing stage-1 overlay for missed elements...")

        try:
            response = run_prompt("chat", review_prompt, image_base64=overlay_url).strip()
            if verbose:
                raw_path = run_path / "sam_review_response.txt"
                raw_path.write_text(response, encoding="utf-8")

            json_text = extract_json_block(response)
            arr_match = re.search(r"\[[\s\S]*?\]", json_text)
            if arr_match:
                parsed = json.loads(arr_match.group(0))
                if isinstance(parsed, list):
                    stage2_prompts = [str(p).strip() for p in parsed if str(p).strip()]
        except Exception as e:
            if verbose:
                logger.warning("  LLM review failed (%s), no additional prompts", e)

    if not stage2_prompts:
        if verbose:
            logger.info("  Stage 2: No additional prompts needed (coverage complete)")
        return {"sam_stage2_prompts": [], "sam_stage2_results": []}

    if verbose:
        logger.info("  Stage 2 additional prompts (%d): %s", len(stage2_prompts), stage2_prompts)

    # --- Run SAM3 with additional prompts ---
    try:
        stage2_detections = _run_sam3_prompts(image_path, stage2_prompts, min_score, verbose)
    except RuntimeError as e:
        if verbose:
            logger.error("  Stage 2 SAM3 failed: %s", e)
        # Stage 2 failure is non-fatal — stage 1 results are sufficient
        return {"sam_stage2_prompts": stage2_prompts, "sam_stage2_results": []}

    # Save stage 2 raw results
    with Image.open(image_path) as _img:
        img_size = _img.size
    raw_data = {
        "image_size": {"width": img_size[0], "height": img_size[1]},
        "results": stage2_detections,
    }
    raw_path = run_path / "sam_stage2_results.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, indent=2, ensure_ascii=False)

    if verbose:
        logger.info("  Stage 2: %d additional detections", len(stage2_detections))

    return {
        "sam_stage2_prompts": stage2_prompts,
        "sam_stage2_results": stage2_detections,
    }


def _classify_boxes_with_llm(valid_boxes, image, run_dir, run_path, verbose):
    """Classify detected boxes using LLM vision (fallback when agent classification unavailable).

    Modifies *valid_boxes* in place — sets corrected_class, is_icon, description,
    is_visualization, and needs_bg_removal on each box.  Spurious boxes are removed.
    Returns the (possibly filtered) list.
    """
    structural_classes, complex_classes = _get_valid_classes()
    _VALID_CLASSES = set(structural_classes) | set(complex_classes)
    # Also accept normalized forms (e.g., "dashed rectangle" → "dashed_rectangle")
    _VALID_CLASSES_NORM = {_normalize_class_name(c) for c in _VALID_CLASSES}

    if len(valid_boxes) == 0:
        return valid_boxes

    if verbose:
        logger.info("  Running LLM classification (fallback)...")

    temp_overlay_path = str(run_path / "sam_all_overlay.png")
    draw_sam_overlay(image, valid_boxes, temp_overlay_path)

    ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    overlay_url = encode_image_to_data_url(temp_overlay_path)

    box_lines = []
    for b in valid_boxes:
        w, h = b["x2"] - b["x1"], b["y2"] - b["y1"]
        box_lines.append(
            f'  {{"label": "{b["label"]}", "sam_class": "{b.get("prompt", "")}", '
            f'"bbox": [{b["x1"]}, {b["y1"]}, {b["x2"]}, {b["y2"]}], '
            f'"size": "{w}x{h}", "score": {b["score"]:.2f}}}'
        )
    box_table = "\n".join(box_lines)

    llm_prompt = (
        "The attached image shows a scientific diagram with bounding boxes labeled <AF>01, <AF>02, etc. "
        "(drawn in semi-transparent colors with labels).\n\n"
        "## Detected Boxes\n"
        f"```json\n[\n{box_table}\n]\n```\n\n"
        "## Task\n"
        "For EACH box, examine what is actually inside it in the image and output a structured classification.\n\n"
        "For each box, determine:\n"
        "1. **corrected_class**: The actual element type you see. The SAM detector's `sam_class` may be wrong — "
        "correct it based on what you visually observe. Valid classes:\n"
        f"   - Structural (SVG-reproducible): {', '.join(structural_classes)}\n"
        f"   - Complex (needs raster image): {', '.join(complex_classes)}\n"
        "2. **is_icon**: true if the content is too complex for basic SVG code (detailed illustrations, "
        "photos, graphs with data points, 3D renders, logos). false for simple shapes, text boxes, "
        "colored rectangles, arrows, lines.\n"
        "3. **description**: Brief description of what you see inside the box (e.g., 'blue rectangle with text SpectrumEncoder', "
        "'right-pointing arrow', 'small stacked rectangles icon').\n"
        "4. **is_visualization**: true if this icon is a data visualization (chart, plot, heatmap, "
        "manifold, attention weights, gradient field, 3D surface, or similar scientific visualization "
        "that can be reproduced with matplotlib/code). false for logos, photos, abstract icons, robots, animals.\n"
        "5. **needs_bg_removal**: true if the icon has a complex non-clean background that should be removed "
        "with BEN2 (colored box behind it, illustration on a non-transparent backdrop). false if the crop "
        "already has a clean white/transparent background or is a flat graphic.\n\n"
        "## Output Format\n"
        "Output ONLY a JSON array, one object per box:\n"
        "```json\n"
        "[\n"
        '  {"label": "<AF>01", "corrected_class": "rectangle", "is_icon": false, "description": "blue box with text", "is_visualization": false, "needs_bg_removal": false},\n'
        '  {"label": "<AF>02", "corrected_class": "icon", "is_icon": true, "description": "manifold plot icon", "is_visualization": true, "needs_bg_removal": true}\n'
        "]\n"
        "```\n"
        "If a box is clearly a SAM detection error (e.g., covers the entire image, or is just background), "
        'set corrected_class to "spurious", is_icon to false, is_visualization to false, and needs_bg_removal to false.'
    )

    if verbose:
        _save_node_prompt(run_dir, "sam3_classification", "", llm_prompt)

    try:
        response = run_prompt("chat", llm_prompt, image_base64=overlay_url).strip()
        if verbose:
            raw_path = Path(run_dir) / "sam_llm_filter_response.txt"
            raw_path.write_text(response, encoding="utf-8")
            logger.debug("  Raw LLM filtering response saved → %s", raw_path.name)

        json_text = extract_json_block(response)
        arr_match = re.search(r"\[[\s\S]*\]", json_text)
        classifications = []
        if arr_match:
            classifications = json.loads(arr_match.group(0))

        if not isinstance(classifications, list):
            classifications = []

        cls_map = {}
        for cls in classifications:
            if isinstance(cls, dict) and "label" in cls:
                cls_map[cls["label"]] = cls

        original_len = len(valid_boxes)
        updated_boxes = []
        icon_labels = []
        for b in valid_boxes:
            cls = cls_map.get(b["label"], {})
            corrected = cls.get("corrected_class", b.get("prompt", ""))
            is_icon = cls.get("is_icon", False)
            desc = cls.get("description", "")
            is_visualization = cls.get("is_visualization", False)
            needs_bg_removal = bool(cls.get("needs_bg_removal", False))

            if corrected == "spurious":
                if verbose:
                    logger.debug("    %s: SPURIOUS (removed) — %s", b["label"], desc)
                continue

            # Normalize class name and validate
            corrected_norm = _normalize_class_name(corrected)
            if corrected not in _VALID_CLASSES and corrected_norm not in _VALID_CLASSES_NORM:
                corrected = _normalize_class_name(b.get("prompt", "rectangle"))
            elif corrected not in _VALID_CLASSES:
                corrected = corrected_norm

            b["corrected_class"] = corrected
            b["is_icon"] = is_icon
            b["description"] = desc
            b["is_visualization"] = bool(is_visualization)
            b["needs_bg_removal"] = needs_bg_removal

            if is_icon:
                icon_labels.append(b["label"])

            updated_boxes.append(b)

        valid_boxes = updated_boxes
        icon_count = sum(1 for b in valid_boxes if b.get("is_icon"))

        if verbose:
            logger.info(
                "  LLM classified %d boxes → %d valid (%d icon, %d structural)",
                original_len,
                len(valid_boxes),
                icon_count,
                len(valid_boxes) - icon_count,
            )
            if icon_labels:
                logger.debug("    Icons: %s", icon_labels)
            for cls in classifications:
                if isinstance(cls, dict):
                    logger.debug(
                        "    %s: %s (icon=%s, viz=%s, bg=%s) — %s",
                        cls.get("label", "?"),
                        cls.get("corrected_class", "?"),
                        cls.get("is_icon", "?"),
                        cls.get("is_visualization", "?"),
                        cls.get("needs_bg_removal", "?"),
                        cls.get("description", "")[:80],
                    )

    except Exception as e:
        if verbose:
            logger.warning("  LLM classification failed (%s), falling back to prompt-based filtering", e)
        for b in valid_boxes:
            b["is_icon"] = b.get("prompt", "").lower() in _get_icon_prompts()
            b.setdefault("corrected_class", b.get("prompt", ""))
            b.setdefault("description", "")
            b.setdefault("is_visualization", False)
            b.setdefault("needs_bg_removal", False)

    return valid_boxes


def sam3_merge_classify_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Merge stage 1 + stage 2 SAM detections, classify, dedup, produce boxlib.

    In OpenCode mode: agent pre-classifies boxes via `valid_boxes` with `corrected_class`
    already set → node skips LLM, just does mechanical merge/dedup.
    In Legacy mode: node calls LLM for classification.

    Output: samed_image_path, boxlib_path, valid_boxes.
    """
    image_path = state.get("generated_image_path", "")
    if not image_path or not Path(image_path).exists():
        return {"error": "No generated image for SAM3 merge", "success": False}

    run_dir = state.get("run_dir", "")
    run_path = Path(run_dir)
    verbose = state.get("verbose", False)

    sam_cfg = _get_sam_config()
    merge_threshold = state.get("sam_merge_threshold", sam_cfg.get("merge_threshold", 0.001))

    with Image.open(image_path) as _img:
        original_size = _img.size
        image = _img.copy()

    # --- Check if agent already provided classified boxes (OpenCode mode) ---
    _AGENT_REQUIRED_FIELDS = ("corrected_class", "is_icon", "description")
    agent_classified = state.get("sam_agent_classified", False)
    agent_boxes = state.get("valid_boxes", []) if agent_classified else []

    # Validate agent boxes have all required fields
    if agent_classified and agent_boxes:
        valid_agent = all(all(b.get(f) is not None for f in _AGENT_REQUIRED_FIELDS) for b in agent_boxes)
        if not valid_agent:
            if verbose:
                logger.warning("  Agent-classified boxes missing required fields, falling back to LLM classification")
            agent_classified = False
            agent_boxes = []

    if agent_classified and agent_boxes:
        # OpenCode mode: agent already classified — use those boxes directly
        valid_boxes = agent_boxes
        all_prompts = state.get("sam_stage1_prompts", []) + state.get("sam_stage2_prompts", [])
        if verbose:
            logger.info("SAM3 Merge: Using %d agent-classified boxes (skipping LLM)", len(valid_boxes))
    else:
        # Legacy mode: build from raw detections + LLM classify
        stage1 = state.get("sam_stage1_results", [])
        stage2 = state.get("sam_stage2_results", [])
        all_detections = stage1 + stage2
        stage1_prompts = state.get("sam_stage1_prompts", [])
        stage2_prompts = state.get("sam_stage2_prompts", [])
        all_prompts = stage1_prompts + stage2_prompts

        if verbose:
            logger.info(
                "SAM3 Merge: %d stage-1 + %d stage-2 = %d total detections",
                len(stage1),
                len(stage2),
                len(all_detections),
            )

        # Build labeled boxes
        valid_boxes = _boxes_from_detections(all_detections)

        # Merge overlapping boxes
        if merge_threshold > 0 and len(valid_boxes) > 1:
            original_count = len(valid_boxes)
            valid_boxes = merge_overlapping_boxes(valid_boxes, merge_threshold)
            if verbose and original_count != len(valid_boxes):
                logger.info("  Merged: %d → %d boxes", original_count, len(valid_boxes))
        elif verbose and merge_threshold <= 0:
            logger.info("  Merging disabled (threshold=%s)", merge_threshold)

        # Reassign IDs after merge
        for i, b in enumerate(valid_boxes):
            b["id"] = i
            b["label"] = f"<AF>{i + 1:02d}"

        # --- LLM CLASSIFICATION (fallback when agent classification unavailable) ---
        valid_boxes = _classify_boxes_with_llm(valid_boxes, image, run_dir, run_path, verbose)

    # Deduplicate boxes with high IoU overlap (>70%)
    valid_boxes = _dedup_boxes_by_iou(
        valid_boxes,
        iou_threshold=0.7,
        verbose=verbose,
        image_path=image_path,
    )

    # Reassign IDs and labels to remain sequential
    for i, b in enumerate(valid_boxes):
        b["id"] = i
        b["label"] = f"<AF>{i + 1:02d}"

    if len(valid_boxes) == 0:
        if verbose:
            logger.warning("  No valid boxes detected")

    # Draw samed image (gray-filled boxes for icon regions only)
    icon_boxes = [b for b in valid_boxes if b.get("is_icon")]
    samed_path = str(run_path / "samed.png")
    draw_samed_image(image, icon_boxes, samed_path)

    # Draw SAM overlay (all boxes, colored)
    sam_overlay_path = str(run_path / "sam_overlay.png")
    draw_sam_overlay(image, valid_boxes, sam_overlay_path)
    if verbose:
        logger.debug("  sam_overlay.png saved")

    # Save boxlib
    boxlib_data = {
        "image_size": {"width": original_size[0], "height": original_size[1]},
        "prompts_used": all_prompts,
        "boxes": valid_boxes,
    }
    boxlib_path = str(run_path / "boxlib.json")
    with open(boxlib_path, "w", encoding="utf-8") as f:
        json.dump(boxlib_data, f, indent=2, ensure_ascii=False)

    if verbose:
        logger.info("  samed.png saved, boxlib.json saved (%d boxes)", len(valid_boxes))

    return {
        "samed_image_path": samed_path,
        "boxlib_path": boxlib_path,
        "valid_boxes": valid_boxes,
    }


def ocr_text_detection_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Run OCR on the generated image to detect text labels.

    Sends each detected SAM3 box region to the OCR service for text recognition.
    The recognized text is attached to each box and passed downstream for SVG generation.
    """
    image_path = state.get("generated_image_path", "")
    valid_boxes = state.get("valid_boxes") or []
    run_dir = state.get("run_dir", "")

    if not image_path or not Path(image_path).exists():
        return {"ocr_texts": []}

    from services.ocr.client import OcrServiceClient

    ocr_url = os.environ.get("OCR_SERVICE_URL", "http://127.0.0.1:8002")
    client = OcrServiceClient(ocr_url)

    abs_image_path = str(Path(image_path).resolve())

    if state.get("verbose"):
        logger.info("OCR text detection: %d regions on %s", len(valid_boxes), Path(image_path).name)

    ocr_texts = []
    try:
        # Full-image OCR to get all text
        result = client.predict(image_path=abs_image_path)
        detections = result.get("items", [])

        for det in detections:
            text = det.get("text", "").strip()
            if not text:
                continue
            confidence = float(det.get("score", 0))
            ocr_conf = load_pipeline_config().get("ocr", {}).get("confidence_threshold", 0.6)
            if confidence < ocr_conf:
                continue

            entry = {"text": text, "confidence": confidence}
            # poly is [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] polygon format
            poly = det.get("poly", [])
            if poly and len(poly) >= 4:
                xs = [p[0] for p in poly]
                ys = [p[1] for p in poly]
                x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
                entry.update({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
                # Estimate font size from the SHORT axis of the bbox.
                # For horizontal text: height ≈ font-size (width >> height).
                # For vertical/rotated text: width ≈ font-size (height >> width).
                # Using min(w, h) handles both cases correctly.
                w, h = x2 - x1, y2 - y1
                entry["font_size_est"] = round(min(w, h))
                # Detect vertical/rotated text: tall-narrow bbox (aspect > 2)
                if h > 0 and w > 0:
                    aspect = max(w, h) / min(w, h)
                    if aspect > 2.0 and h > w:
                        entry["rotation"] = -90
                    else:
                        entry["rotation"] = 0
                else:
                    entry["rotation"] = 0
            ocr_texts.append(entry)

        if state.get("verbose"):
            logger.info("  OCR detected %d text region(s)", len(ocr_texts))

        # Bidirectional text-box association:
        #   box["texts"]    — list of OCR entries whose center falls inside the box
        #   ocr["boxes"]    — list of box labels that contain this text
        for ocr in ocr_texts:
            ocr["boxes"] = []
        for box in valid_boxes:
            contained: list = []
            bx1, by1, bx2, by2 = box["x1"], box["y1"], box["x2"], box["y2"]
            for ocr in ocr_texts:
                if "x1" not in ocr:
                    continue
                ocr_cx = (ocr["x1"] + ocr["x2"]) / 2
                ocr_cy = (ocr["y1"] + ocr["y2"]) / 2
                if bx1 <= ocr_cx <= bx2 and by1 <= ocr_cy <= by2:
                    contained.append(ocr)
                    ocr["boxes"].append(box["label"])
            box["texts"] = contained
            # Legacy flat string kept for backward compat
            box["ocr_text"] = " ".join(e["text"] for e in contained)

    except Exception as e:
        if state.get("verbose"):
            logger.warning("  OCR failed (%s), continuing without text detection", e)
        # Non-fatal: pipeline can still generate SVG without OCR text

    # Save OCR results + overlay
    if run_dir:
        ocr_path = Path(run_dir) / "ocr_results.json"
        ocr_path.write_text(json.dumps(ocr_texts, indent=2, ensure_ascii=False), encoding="utf-8")

        if ocr_texts and image_path and Path(image_path).exists():
            ocr_overlay_path = str(Path(run_dir) / "ocr_overlay.png")
            with Image.open(image_path) as _ocr_img:
                draw_ocr_overlay(_ocr_img.copy(), ocr_texts, ocr_overlay_path)
            if state.get("verbose"):
                logger.debug("  ocr_overlay.png saved")

    return {
        "ocr_texts": ocr_texts,
        "valid_boxes": valid_boxes,  # Updated with ocr_text field
    }


def icon_extraction_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Crop detected icon regions and remove backgrounds via the BEN2 service.

    Only boxes marked as is_icon=True by the LLM classifier are extracted;
    structural boxes are skipped. Falls back to prompt-based filtering
    if the is_icon field is absent.
    """
    image_path = state.get("generated_image_path", "")
    boxlib_path = state.get("boxlib_path", "")
    run_dir = state.get("run_dir", "")

    if not image_path or not boxlib_path:
        return {"icon_infos": []}

    run_path = Path(run_dir)
    icons_dir = run_path / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(image_path) as _img:
        image = _img.copy()
    with open(boxlib_path, "r", encoding="utf-8") as f:
        boxlib_data = json.load(f)
    all_boxes = boxlib_data.get("boxes", [])

    # Filter to icon boxes only (LLM-classified is_icon, or prompt-based fallback)
    boxes = [b for b in all_boxes if b.get("is_icon", b.get("prompt", "").lower() in _get_icon_prompts())]

    if not boxes:
        if state.get("verbose"):
            logger.info("No icon boxes — skipping icon extraction")
        return {"icon_infos": []}

    boxes_needing_bg_removal = [b for b in boxes if b.get("needs_bg_removal", False)]
    if boxes_needing_bg_removal:
        try:
            from services.ben2.client import BEN2ServiceClient

            ben2_endpoint = os.environ.get(
                "BEN2_SERVICE_URL",
                load_pipeline_config().get("services", {}).get("ben2_endpoint", "http://127.0.0.1:8003"),
            )
            ben2_client = BEN2ServiceClient(ben2_endpoint)
            use_ben2 = ben2_client.health()
        except Exception as e:
            if state.get("verbose"):
                logger.warning("BEN2 service unavailable (%s), cropping without background removal", e)
            ben2_client = None
            use_ben2 = False
    else:
        ben2_client = None
        use_ben2 = False

    icon_infos = []
    for box_info in boxes:
        label = box_info.get("label", f"<AF>{box_info['id'] + 1:02d}")
        label_clean = label.replace("<", "").replace(">", "")
        x1, y1, x2, y2 = box_info["x1"], box_info["y1"], box_info["x2"], box_info["y2"]

        cropped = image.crop((x1, y1, x2, y2))
        crop_path = str(icons_dir / f"icon_{label_clean}.png")
        cropped.save(crop_path)

        needs_bg_removal = bool(box_info.get("needs_bg_removal", False))
        if needs_bg_removal and use_ben2 and ben2_client is not None:
            try:
                foreground = ben2_client.remove_background_region(
                    image_path,
                    x1,
                    y1,
                    x2,
                    y2,
                )
                nobg_path = str(icons_dir / f"icon_{label_clean}_nobg.png")
                foreground.save(nobg_path)
            except Exception as e:
                if state.get("verbose"):
                    logger.warning("BEN2 remove_background failed for %s (%s), using raw crop", label, e)
                nobg_path = str(icons_dir / f"icon_{label_clean}_nobg.png")
                cropped.convert("RGBA").save(nobg_path)
        else:
            nobg_path = str(icons_dir / f"icon_{label_clean}_nobg.png")
            cropped.convert("RGBA").save(nobg_path)

        icon_infos.append(
            {
                "id": box_info["id"],
                "label": label,
                "label_clean": label_clean,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "width": x2 - x1,
                "height": y2 - y1,
                "score": box_info.get("score", 0.0),
                "prompt": box_info.get("prompt", ""),
                "corrected_class": box_info.get("corrected_class", box_info.get("prompt", "")),
                "is_icon": box_info.get("is_icon", True),
                "is_visualization": box_info.get("is_visualization", False),
                "needs_bg_removal": needs_bg_removal,
                "description": box_info.get("description", ""),
                "crop_path": crop_path,
                "nobg_path": nobg_path,
            }
        )

    if state.get("verbose"):
        ben2_count = sum(1 for icon in icon_infos if icon.get("needs_bg_removal"))
        logger.info("Extracted %d icon(s) (%d requested BEN2 background removal)", len(icon_infos), ben2_count)

    return {"icon_infos": icon_infos}


def architecture_review_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Review the generated raster figure before segmentation and SVG stages."""
    image_path = state.get("generated_image_path", "")
    if not image_path or not Path(image_path).exists():
        return {"error": "No generated image for architecture review", "success": False}

    ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    run_dir = state.get("run_dir", "")
    method_description = state.get("method_description", "")
    min_score = float(load_pipeline_config().get("architecture_review", {}).get("min_score", 6.0))
    arch_iter = state.get("architecture_review_iteration", 0)

    system_prompt = _load_prompt("architecture_review_system.md")
    user_prompt = (
        "Method description:\n"
        f"{method_description[:4000]}\n\n"
        "Review the attached raster figure only. Check structural completeness, layout clarity, and "
        "obvious duplicate or missing modules. Ignore SVG concerns.\n\n"
        f"Set `pass` to true only if the figure is readable, structurally complete, and deserves at least {min_score}/10.\n"
        "Return only JSON:\n"
        "{\n"
        '  "pass": true,\n'
        '  "score": 8.0,\n'
        '  "feedback": "Short summary.",\n'
        '  "missing_components": ["decoder"],\n'
        '  "issues": ["flow direction is ambiguous"]\n'
        "}\n"
    )

    if state.get("verbose"):
        _save_node_prompt(run_dir, "architecture_review", system_prompt, user_prompt, suffix=f"v{arch_iter + 1}")

    try:
        response = run_prompt(
            "chat",
            user_prompt,
            system_prompt=system_prompt,
            image_base64=encode_image_to_data_url(image_path),
        ).strip()
        json_text = extract_json_block(response)
        parsed = json.loads(json_text)
        if not isinstance(parsed, dict):
            raise ValueError("Architecture review did not return a JSON object")

        score = float(parsed.get("score", 0.0))
        missing_components = [str(item).strip() for item in parsed.get("missing_components", []) if str(item).strip()]
        issues = [str(item).strip() for item in parsed.get("issues", []) if str(item).strip()]
        normalized = {
            "pass": bool(parsed.get("pass", False)) and score >= min_score,
            "score": score,
            "feedback": str(parsed.get("feedback", "")).strip(),
            "missing_components": missing_components,
            "issues": issues,
        }
    except Exception as e:
        normalized = {
            "pass": True,
            "score": min_score,
            "feedback": f"Architecture review skipped after error: {e}",
            "missing_components": [],
            "issues": [],
        }

    if state.get("verbose"):
        verdict = "pass" if normalized.get("pass") else "fail"
        logger.info("Architecture review: %s (%.1f/10)", verdict, normalized.get("score", 0))

    if run_dir:
        out_path = Path(run_dir) / f"architecture_review_v{arch_iter + 1}.json"
        out_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")

    return {"architecture_review_feedback": normalized}


def _strip_code_fences(code: str) -> str:
    if code.startswith("```"):
        lines = code.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        code = "\n".join(lines)
    return code


def visualization_code_gen_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Code-generate visualization-like icon regions and replace their nobg paths."""
    icon_infos = list(state.get("icon_infos") or [])
    run_dir = state.get("run_dir", "")
    if not icon_infos or not run_dir:
        return {"visualization_icons": [], "codegen_icon_paths": {}}

    viz_indices = [i for i, icon in enumerate(icon_infos) if icon.get("is_visualization")]
    if not viz_indices:
        if state.get("verbose"):
            logger.info("No visualization icons detected — skipping codegen replacement")
        return {"visualization_icons": [], "codegen_icon_paths": {}}

    ensure_gpt()
    from llm import run_prompt

    few_shots = _load_style_few_shots(str(VISUALIZATION_EXAMPLES_DIR))
    few_shot_messages = _build_style_few_shot_messages(few_shots[:4]) if few_shots else None
    codegen_dir = Path(run_dir) / "icons_codegen"
    codegen_dir.mkdir(parents=True, exist_ok=True)

    codegen_icon_paths: dict[str, str] = {}
    visualization_icons: list[dict] = []

    for idx in viz_indices:
        icon = dict(icon_infos[idx])
        label_clean = icon.get("label_clean", f"icon_{idx}")
        w = max(64, int(icon.get("width", 128)))
        h = max(64, int(icon.get("height", 128)))
        desc = (icon.get("description") or "").strip()
        out_path = codegen_dir / f"icon_{label_clean}_codegen.png"
        code_path = codegen_dir / f"icon_{label_clean}_codegen.py"

        user_prompt = (
            "Generate a standalone Python matplotlib script for a scientific visualization icon.\n"
            f"Icon description: {desc or 'scientific visualization icon'}\n"
            f"Target size (pixels): width={w}, height={h}\n"
            "Requirements:\n"
            "- Use matplotlib and/or numpy only.\n"
            "- Produce a clean icon-like visualization with transparent background.\n"
            "- Remove axes, ticks, labels, and borders.\n"
            "- Use tight layout and preserve the requested aspect ratio.\n"
            f"- Save to this exact path: {str(out_path)!r}\n"
            "- Print exactly one line: Saved: <output_path>\n"
            "Return only Python code."
        )
        if state.get("verbose"):
            _save_node_prompt(
                run_dir,
                "visualization_icon_codegen",
                "",
                user_prompt,
                suffix=label_clean,
            )

        generated_code = ""
        try:
            generated_code = run_prompt(
                "code",
                user_prompt,
                few_shot_messages=few_shot_messages,
            ).strip()
        except Exception as e:
            if state.get("verbose"):
                logger.error("Visualization codegen failed for %s: %s", label_clean, e)
            visualization_icons.append({"label_clean": label_clean, "generated": False, "error": str(e)})
            continue

        generated_code = _strip_code_fences(generated_code)
        code_path.write_text(generated_code, encoding="utf-8")

        exec_result = subprocess.run(
            [os.environ.get("PYTHON_BIN", "python"), str(code_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if exec_result.returncode != 0 or not out_path.exists():
            if state.get("verbose"):
                logger.error(
                    "Visualization icon execution failed for %s: %s",
                    label_clean,
                    (exec_result.stderr or exec_result.stdout or "").strip()[:200],
                )
            visualization_icons.append(
                {
                    "label_clean": label_clean,
                    "generated": False,
                    "error": (exec_result.stderr or exec_result.stdout or "").strip(),
                }
            )
            continue

        icon["nobg_path"] = str(out_path)
        icon["codegen_path"] = str(out_path)
        icon_infos[idx] = icon
        codegen_icon_paths[label_clean] = str(out_path)
        visualization_icons.append({"label_clean": label_clean, "generated": True, "path": str(out_path)})

    if state.get("verbose"):
        logger.info("Visualization icon codegen: %d/%d icons replaced", len(codegen_icon_paths), len(viz_indices))

    return {
        "icon_infos": icon_infos,
        "visualization_icons": visualization_icons,
        "codegen_icon_paths": codegen_icon_paths,
    }


def svg_generation_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Generate SVG template via multimodal LLM using figure.png + samed.png."""
    ensure_gpt()
    from llm import run_prompt

    image_path = state.get("generated_image_path", "")
    samed_path = state.get("samed_image_path", "")
    run_dir = state.get("run_dir", "")
    icon_infos = state.get("icon_infos") or []

    if not image_path:
        return {"error": "Missing image path for SVG generation", "success": False}

    with Image.open(image_path) as _fig:
        figure_img = _fig.copy()
    fw, fh = figure_img.size

    # Build composite image for the llm module (single image support)
    # Skip samed panel when no icon regions detected (samed is identical to original)
    if icon_infos and samed_path:
        with Image.open(samed_path) as _sam:
            samed_img = _sam.copy()
        composite = build_composite_image(figure_img, samed_img, direction="horizontal")
        samed_img.close()
        composite_path = Path(run_dir) / "composite_for_svg_gen.png"
        composite.save(str(composite_path))
        figure_img.close()
        composite.close()
    else:
        composite_path = Path(image_path)

    from llm import encode_image_to_data_url

    composite_url = encode_image_to_data_url(str(composite_path))

    system_prompt = _load_prompt(
        "svg_generation_system.md",
        figure_width=str(fw),
        figure_height=str(fh),
    )

    # Build structured box + OCR context
    valid_boxes = state.get("valid_boxes") or []
    ocr_texts = state.get("ocr_texts") or []
    box_context = _build_box_context(valid_boxes, icon_infos)
    ocr_context = _build_ocr_context(ocr_texts)
    num_boxes = len(valid_boxes)

    # Build user prompt
    if icon_infos:
        image_desc = [
            "The attached image is a composite showing two views side by side:",
            "- LEFT: The target layout and style.",
            "- RIGHT: The exact same figure, but with specific regions covered by gray placeholder boxes labeled <AF>01, <AF>02, etc.",
        ]
    else:
        image_desc = [
            "The attached image shows the target figure layout and style.",
            "No icon regions were detected, so replicate the entire figure with SVG elements.",
        ]
    user_parts = image_desc + [
        "",
        f"The output SVG dimensions should be exactly {fw} x {fh} pixels (ONLY ONE PANEL).",
        "",
        "CRITICAL INSTRUCTIONS:",
        "1. You must generate a SINGLE unified diagram. DO NOT generate a side-by-side view.",
        "2. Look at the LEFT image for the complete visual reference.",
        "3. Each detected region below is classified as STRUCTURAL or ICON:",
        "   - STRUCTURAL regions: Replicate these with SVG code (rect, path, line, text, polygon, etc.) "
        "matching the colors, shapes, and positions visible in the LEFT image.",
        '   - ICON regions: Create a gray placeholder <g id="AFxx"> with <rect fill="#808080" stroke="black"/> '
        "and centered white <text> label. These will be replaced with actual images later.",
        "4. Use the exact coordinates provided below for positioning.",
        "",
        box_context,
    ]

    if ocr_context:
        user_parts.append("")
        user_parts.append(ocr_context)

    user_parts.append(
        f"\nThere are {num_boxes} detected regions. Include each region ONCE. "
        "If two regions cover the same visual element (similar coordinates), include only the one with the better description. "
        'For ICON regions, use <g id="AFxx"><rect fill="#808080" .../><text>AFxx</text></g>. '
        "For STRUCTURAL regions, replicate the visual appearance from the LEFT image using SVG elements. "
        "Focus on reproducing ALL arrows and connectors visible in the original — arrows are critical for showing data flow."
    )

    user_parts.append("\nOutput ONLY the SVG code, starting with <svg and ending with </svg>.")
    user_text = "\n".join(user_parts)

    if state.get("verbose"):
        from llm import get_backend, get_model_display

        logger.info("Generating SVG template (backend=%s, model=%s)...", get_backend(), get_model_display("chat"))
        _save_node_prompt(run_dir, "svg_generation", system_prompt, user_text)

    for attempt in range(3):
        try:
            response = run_prompt(
                "chat",
                user_text,
                system_prompt=system_prompt,
                image_base64=composite_url,
            ).strip()
            break
        except Exception as e:
            if attempt < 2:
                _time.sleep(10 * (attempt + 1))
            else:
                return {"error": f"SVG generation failed: {e}", "success": False}

    # Save raw LLM response before extraction
    if state.get("verbose"):
        raw_path = Path(run_dir) / "svg_generation_raw_response.txt"
        raw_path.write_text(response, encoding="utf-8")
        logger.debug("  Raw LLM response saved (%d chars) → %s", len(response), raw_path.name)

    svg_code = extract_svg_code(response)
    if not svg_code:
        return {"error": "Could not extract SVG from LLM response", "success": False}

    template_path = str(Path(run_dir) / "template.svg")
    Path(template_path).write_text(svg_code, encoding="utf-8")

    # Render template SVG to PNG for inspection
    if state.get("verbose"):
        logger.info("  Template SVG saved (%d chars)", len(svg_code))
        template_png = str(Path(run_dir) / "template_preview.png")
        render_result = svg_to_png(template_path, template_png)
        if render_result:
            logger.debug("  Template preview rendered → template_preview.png")
            _print_svg_diff(svg_code, template_png, valid_boxes, ocr_texts, "svg_gen")
        else:
            logger.warning("  Template preview render failed (cairosvg not available?)")

    return {
        "svg_code": svg_code,
        "template_svg_path": template_path,
    }


def svg_validation_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Validate SVG syntax using lxml. Sets svg_valid and svg_errors."""
    svg_code = state.get("svg_code", "")
    if not svg_code:
        return {"svg_valid": False, "svg_errors": ["No SVG code available"]}

    is_valid, errors = validate_svg_syntax(svg_code)

    # Check for text boundary overflow (warnings, not fatal)
    text_warnings = validate_text_boundaries(svg_code)
    if text_warnings:
        errors.extend(text_warnings)

    if state.get("verbose"):
        if is_valid:
            dims = get_svg_dimensions(svg_code)
            n_b64 = count_base64_images(svg_code)
            logger.info(
                "  SVG syntax valid (dims=%sx%s, %d base64 images, %d chars)", dims[0], dims[1], n_b64, len(svg_code)
            )
            if text_warnings:
                logger.warning("  Text boundary warnings (%d):", len(text_warnings))
                for w in text_warnings[:5]:
                    logger.warning("    - %s", w)
        else:
            logger.error("  SVG has %d syntax error(s):", len(errors))
            for err in errors[:5]:
                logger.error("    - %s", err)

    return {"svg_valid": is_valid, "svg_errors": errors}


def svg_fix_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """LLM-based SVG syntax repair."""
    ensure_gpt()
    from llm import run_prompt

    svg_code = state.get("svg_code", "")
    errors = state.get("svg_errors", [])
    fix_iter = state.get("svg_fix_iteration", 0)

    if state.get("verbose"):
        logger.info("  Fixing SVG (attempt %d)...", fix_iter + 1)

    error_list = "\n".join(f"  - {e}" for e in errors)
    system_prompt = _load_prompt("svg_fix_system.md", errors=error_list)

    user_text = (
        f"Fix the following SVG code:\n\n```xml\n{svg_code}\n```\n\n"
        f"Errors:\n{error_list}\n\n"
        "Output ONLY the fixed SVG code."
    )

    if state.get("verbose"):
        _save_node_prompt(state.get("run_dir", ""), "svg_fix", system_prompt, user_text, suffix=f"v{fix_iter}")

    for attempt in range(3):
        try:
            response = run_prompt("chat", user_text, system_prompt=system_prompt).strip()
            break
        except Exception:
            if attempt < 2:
                _time.sleep(10 * (attempt + 1))
            else:
                return {"svg_fix_iteration": fix_iter + 1}

    # Save raw fix response
    run_dir = state.get("run_dir", "")
    if state.get("verbose") and run_dir:
        raw_path = Path(run_dir) / f"svg_fix_raw_response_v{fix_iter}.txt"
        raw_path.write_text(response, encoding="utf-8")
        logger.debug("    Raw fix response saved → %s", raw_path.name)

    fixed_svg = extract_svg_code(response)
    if fixed_svg:
        if state.get("verbose") and run_dir:
            fixed_path = Path(run_dir) / f"svg_fixed_v{fix_iter}.svg"
            fixed_path.write_text(fixed_svg, encoding="utf-8")
            logger.debug("    Fixed SVG saved (%d chars) → %s", len(fixed_svg), fixed_path.name)
        return {"svg_code": fixed_svg, "svg_fix_iteration": fix_iter + 1}

    if state.get("verbose"):
        logger.warning("    Could not extract SVG from fix response")
    return {"svg_fix_iteration": fix_iter + 1}


def svg_optimization_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Iterative LLM-based SVG optimization to align with the original figure."""
    ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    max_iters = state.get("optimize_iterations", 2)
    if max_iters == 0:
        if state.get("verbose"):
            logger.info("  SVG optimization skipped (iterations=0)")
        # Copy template as optimized
        template = state.get("template_svg_path", "")
        run_dir = state.get("run_dir", "")
        if template and run_dir:
            opt_path = str(Path(run_dir) / "optimized_template.svg")
            shutil.copy(template, opt_path)
            return {"optimized_svg_path": opt_path}
        return {}

    svg_code = state.get("svg_code", "")
    image_path = state.get("generated_image_path", "")
    samed_path = state.get("samed_image_path", "")
    run_dir = state.get("run_dir", "")

    if not svg_code or not image_path or not samed_path:
        return {}

    run_path = Path(run_dir)
    system_prompt = _load_prompt("svg_optimize_system.md")
    current_svg = svg_code
    last_check_summary = ""  # post-render check results from previous iteration

    for iteration in range(max_iters):
        if state.get("verbose"):
            logger.info("  Optimization iteration %d/%d", iteration + 1, max_iters)

        # Render current SVG to PNG
        temp_svg = run_path / f"_opt_iter_{iteration}.svg"
        temp_png = run_path / f"_opt_iter_{iteration}.png"
        temp_svg.write_text(current_svg, encoding="utf-8")

        png_result = svg_to_png(str(temp_svg), str(temp_png))
        if png_result is None:
            if state.get("verbose"):
                logger.warning("    Cannot render SVG to PNG, stopping optimization")
            break

        # Build composite: figure + samed (if icons) + current rendering
        with Image.open(image_path) as _fig:
            figure_img = _fig.copy()
        with Image.open(str(temp_png)) as _cur:
            current_img = _cur.copy()

        icon_infos = state.get("icon_infos") or []
        valid_boxes = state.get("valid_boxes") or []
        if icon_infos and samed_path:
            with Image.open(samed_path) as _sam:
                samed_img = _sam.copy()
            top_composite = build_composite_image(figure_img, samed_img)
            full_composite = build_composite_image(top_composite, current_img)
            samed_img.close()
            top_composite.close()
        else:
            full_composite = build_composite_image(figure_img, current_img)

        comp_path = run_path / f"_opt_composite_{iteration}.png"
        full_composite.save(str(comp_path))
        figure_img.close()
        current_img.close()
        full_composite.close()
        comp_url = encode_image_to_data_url(str(comp_path))

        # Build context: strip base64 from SVG, provide box + OCR data
        svg_for_llm = _strip_base64_from_svg(current_svg)
        ocr_texts = state.get("ocr_texts") or []
        box_context = _build_box_context(valid_boxes, icon_infos)
        ocr_context = _build_ocr_context(ocr_texts)

        if icon_infos:
            image_desc = [
                "The attached image shows three views side by side:",
                "- LEFT: original figure (target)",
                "- MIDDLE: annotated figure with icon regions marked",
                "- RIGHT: current SVG rendering",
            ]
        else:
            image_desc = [
                "The attached image shows two views side by side:",
                "- LEFT: original figure (target)",
                "- RIGHT: current SVG rendering",
            ]
        opt_parts = image_desc + [
            "",
            f"Current SVG code (base64 image data stripped for brevity):\n```xml\n{svg_for_llm}\n```",
            "",
            box_context,
        ]
        if ocr_context:
            opt_parts.append("")
            opt_parts.append(ocr_context)
        if last_check_summary:
            opt_parts.append("")
            opt_parts.append("## Automated Post-Render Check Results (from previous iteration)")
            opt_parts.append(last_check_summary)
            opt_parts.append("")
            opt_parts.append(
                "FIX the issues above before making other changes. These are concrete, programmatically-detected errors."
            )
        opt_parts.extend(
            [
                "",
                "CRITICAL INSTRUCTIONS:",
                "1. Optimize the SVG to better match the LEFT (original) figure.",
                "2. Output ONLY the optimized SVG code for a SINGLE unified diagram.",
                "3. STRUCTURAL regions: improve their SVG code (colors, shapes, positions) to match the original.",
                '4. ICON placeholder regions: keep them as <g id="AFxx"> gray boxes — do NOT modify or remove.',
                '5. Where the SVG has href="...BASE64_DATA_STRIPPED...", preserve that <image> tag exactly as-is.',
            ]
        )
        user_text = "\n".join(opt_parts)

        if state.get("verbose"):
            _save_node_prompt(run_dir, "svg_optimization", system_prompt, user_text, suffix=f"iter{iteration}")

        try:
            response = run_prompt(
                "chat",
                user_text,
                system_prompt=system_prompt,
                image_base64=comp_url,
            ).strip()
        except Exception as e:
            if state.get("verbose"):
                logger.error("    Optimization failed: %s", e)
            break

        # Save raw optimization response
        if state.get("verbose"):
            raw_path = run_path / f"opt_raw_response_iter{iteration}.txt"
            raw_path.write_text(response, encoding="utf-8")
            logger.debug("    Raw optimization response saved (%d chars) → %s", len(response), raw_path.name)

        optimized = extract_svg_code(response)
        if not optimized:
            if state.get("verbose"):
                logger.warning("    Could not extract SVG from optimization response")
            break

        # Restore base64 data that was stripped for the LLM
        b64_count = count_base64_images(current_svg)
        if b64_count > 0:
            optimized = _restore_base64_in_svg(optimized, current_svg)
            if state.get("verbose"):
                restored = count_base64_images(optimized)
                logger.debug("    Restored base64 images: %d/%d", restored, b64_count)

        # Validate syntax
        is_valid, errors = validate_svg_syntax(optimized)
        if not is_valid:
            if state.get("verbose"):
                logger.error("    Optimized SVG has syntax errors: %s", errors[:3])
                err_path = run_path / f"opt_invalid_iter{iteration}.svg"
                err_path.write_text(optimized, encoding="utf-8")
                logger.debug("    Invalid SVG saved → %s", err_path.name)
            continue

        current_svg = optimized

        # Run structured post-render checks
        ocr_texts = state.get("ocr_texts") or []
        check_result = run_post_render_checks(current_svg, valid_boxes, ocr_texts)
        last_check_summary = check_result["summary"]

        # Persist check results as JSON
        check_json_path = run_path / f"opt_checks_iter{iteration}.json"
        try:
            check_json_path.write_text(
                json.dumps(check_result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # Non-critical: skip saving check results if write fails

        if state.get("verbose"):
            logger.info("    Post-render checks: %d issue(s)", len(check_result["issues"]))
            if check_result["has_critical"]:
                logger.warning("    Critical issues detected — will inject into next iteration prompt")

        # Save each valid iteration's SVG, render preview, and track diff
        if state.get("verbose"):
            iter_svg_path = run_path / f"opt_valid_iter{iteration}.svg"
            iter_svg_path.write_text(current_svg, encoding="utf-8")
            iter_png_path = run_path / f"opt_preview_iter{iteration}.png"
            render_ok = svg_to_png(str(iter_svg_path), str(iter_png_path))
            if render_ok:
                logger.debug("    Optimized SVG iter %d preview → %s", iteration, iter_png_path.name)
                _print_svg_diff(
                    current_svg,
                    str(iter_png_path),
                    valid_boxes,
                    state.get("ocr_texts") or [],
                    f"opt_iter{iteration}",
                )

        # Clean up temp files (keep verbose artifacts above)
        try:
            temp_svg.unlink(missing_ok=True)
            temp_png.unlink(missing_ok=True)
            comp_path.unlink(missing_ok=True)
        except OSError:
            pass  # Non-critical: temp file cleanup best-effort

    # Save optimized SVG
    opt_path = str(run_path / "optimized_template.svg")
    Path(opt_path).write_text(current_svg, encoding="utf-8")

    if state.get("verbose"):
        logger.info("  Optimized SVG saved (%d chars)", len(current_svg))
        # Render final optimized preview
        opt_png = str(run_path / "optimized_preview.png")
        if svg_to_png(opt_path, opt_png):
            logger.debug("  Optimized preview rendered → optimized_preview.png")

    return {
        "svg_code": current_svg,
        "optimized_svg_path": opt_path,
    }


def icon_replacement_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Replace icon placeholders in SVG with base64-encoded transparent PNGs."""
    svg_code = state.get("svg_code", "")
    icon_infos = state.get("icon_infos") or []
    run_dir = state.get("run_dir", "")

    if not svg_code:
        return {"error": "No SVG code for icon replacement", "success": False}

    if not icon_infos:
        if state.get("verbose"):
            logger.info("  No icons to replace — using SVG as-is")
        final_path = str(Path(run_dir) / "final.svg")
        Path(final_path).write_text(svg_code, encoding="utf-8")
        return {"svg_code": svg_code, "final_svg_path": final_path}

    # Compute scale factors
    image_path = state.get("generated_image_path", "")
    if image_path and Path(image_path).exists():
        with Image.open(image_path) as fig_img:
            fw, fh = fig_img.size
        sw, sh = get_svg_dimensions(svg_code)
        if sw and sh:
            if abs(sw - fw) < 1 and abs(sh - fh) < 1:
                scale_x, scale_y = 1.0, 1.0
            else:
                scale_x, scale_y = calculate_scale_factors(fw, fh, sw, sh)
        else:
            scale_x, scale_y = 1.0, 1.0
    else:
        scale_x, scale_y = 1.0, 1.0

    if state.get("verbose"):
        logger.info("  Icon replacement: %d icons, scale=(%.3f, %.3f)", len(icon_infos), scale_x, scale_y)
        # Render SVG before icon replacement for comparison
        pre_icon_svg = str(Path(run_dir) / "pre_icon_replacement.svg")
        Path(pre_icon_svg).write_text(svg_code, encoding="utf-8")
        pre_icon_png = str(Path(run_dir) / "pre_icon_replacement.png")
        if svg_to_png(pre_icon_svg, pre_icon_png):
            logger.debug("  Pre-icon-replacement preview → pre_icon_replacement.png")

    svg_content = svg_code

    for icon in icon_infos:
        label = icon.get("label", "")
        label_clean = icon.get("label_clean", label.replace("<", "").replace(">", ""))
        nobg_path = icon["nobg_path"]

        # Skip if this icon is already embedded as a base64 <image> in the SVG
        existing_icon_pat = rf'<image[^>]*\bid=["\']?icon_{re.escape(label_clean)}["\']?[^>]*href=["\']data:image/'
        if re.search(existing_icon_pat, svg_content, re.IGNORECASE):
            if state.get("verbose"):
                logger.debug("    Skipping %s — already embedded in SVG", label_clean)
            continue

        # Skip if no placeholder exists in the current SVG (may have been
        # removed by optimization or refinement as a duplicate)
        # Check for: <g id="AFxx">, <text>&lt;AF&gt;xx</text>, <text>AFxx</text>
        g_pat = rf'<g[^>]*\bid=["\']?{re.escape(label_clean)}["\']?'
        text_pat = rf"(?:&lt;AF&gt;|<AF>|(?<=[>\s])){re.escape(label_clean)}(?=[<\s])"
        if not re.search(g_pat, svg_content, re.IGNORECASE) and not re.search(text_pat, svg_content, re.IGNORECASE):
            if state.get("verbose"):
                logger.debug("    Skipping %s — no placeholder found in SVG", label_clean)
            continue

        with Image.open(nobg_path) as icon_img:
            buf = io.BytesIO()
            icon_img.save(buf, format="PNG")
        icon_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        replaced = False

        # Method 1: Match <g id="AFxx"> group
        g_pattern = rf'<g[^>]*\bid=["\']?{re.escape(label_clean)}["\']?[^>]*>[\s\S]*?</g>'
        g_match = re.search(g_pattern, svg_content, re.IGNORECASE)

        if g_match:
            g_content = g_match.group(0)

            # Extract translate transform
            tx, ty = 0.0, 0.0
            g_tag = re.match(r"<g[^>]*>", g_content, re.IGNORECASE)
            if g_tag:
                tm = re.search(
                    r'transform=["\'][^"\']*translate\s*\(\s*([\d.-]+)[\s,]+([\d.-]+)\s*\)',
                    g_tag.group(0),
                    re.IGNORECASE,
                )
                if tm:
                    tx, ty = float(tm.group(1)), float(tm.group(2))

            # Extract rect dimensions
            rect_patterns = [
                r'<rect[^>]*\bx=["\']?([\d.]+)["\']?[^>]*\by=["\']?([\d.]+)["\']?[^>]*\bwidth=["\']?([\d.]+)["\']?[^>]*\bheight=["\']?([\d.]+)["\']?',
                r'<rect[^>]*\bwidth=["\']?([\d.]+)["\']?[^>]*\bheight=["\']?([\d.]+)["\']?[^>]*\bx=["\']?([\d.]+)["\']?[^>]*\by=["\']?([\d.]+)["\']?',
            ]

            for rp_idx, rp in enumerate(rect_patterns):
                rm = re.search(rp, g_content, re.IGNORECASE)
                if rm:
                    groups = rm.groups()
                    if rp_idx == 1:
                        w, h, x, y = groups
                    else:
                        x, y, w, h = groups
                    x, y, w, h = float(x) + tx, float(y) + ty, float(w), float(h)

                    image_tag = (
                        f'<image id="icon_{label_clean}" x="{x}" y="{y}" '
                        f'width="{w}" height="{h}" '
                        f'href="data:image/png;base64,{icon_b64}" '
                        f'preserveAspectRatio="xMidYMid meet"/>'
                    )
                    svg_content = svg_content.replace(g_content, image_tag)
                    replaced = True
                    if state.get("verbose"):
                        logger.debug(
                            "    %s: replaced via <g> group match (x=%s, y=%s, w=%s, h=%s)", label_clean, x, y, w, h
                        )
                    break

        # Method 2: Match <text> containing the label
        if not replaced:
            text_patterns = [
                rf"<text[^>]*>[^<]*{re.escape(label)}[^<]*</text>",
                rf"<text[^>]*>[^<]*&lt;AF&gt;{label_clean[2:]}[^<]*</text>",
            ]
            for tp in text_patterns:
                tm = re.search(tp, svg_content, re.IGNORECASE)
                if tm:
                    text_pos = tm.start()
                    preceding = svg_content[:text_pos]
                    rect_matches = list(re.finditer(r"<rect[^>]*/?\s*>", preceding, re.IGNORECASE))
                    if rect_matches:
                        rect_content = rect_matches[-1].group(0)
                        xm = re.search(r'\bx=["\']?([\d.]+)', rect_content)
                        ym = re.search(r'\by=["\']?([\d.]+)', rect_content)
                        wm = re.search(r'\bwidth=["\']?([\d.]+)', rect_content)
                        hm = re.search(r'\bheight=["\']?([\d.]+)', rect_content)
                        if all([xm, ym, wm, hm]):
                            x = float(xm.group(1))
                            y = float(ym.group(1))
                            w = float(wm.group(1))
                            h = float(hm.group(1))
                            image_tag = (
                                f'<image id="icon_{label_clean}" x="{x}" y="{y}" '
                                f'width="{w}" height="{h}" '
                                f'href="data:image/png;base64,{icon_b64}" '
                                f'preserveAspectRatio="xMidYMid meet"/>'
                            )
                            svg_content = svg_content.replace(tm.group(0), "")
                            svg_content = svg_content.replace(rect_content, image_tag, 1)
                            replaced = True
                            if state.get("verbose"):
                                logger.debug(
                                    "    %s: replaced via <text> match (x=%s, y=%s, w=%s, h=%s)",
                                    label_clean,
                                    x,
                                    y,
                                    w,
                                    h,
                                )
                            break

        # Fallback: append at original coordinates
        if not replaced:
            x1 = icon["x1"] * scale_x
            y1 = icon["y1"] * scale_y
            w = icon["width"] * scale_x
            h = icon["height"] * scale_y
            image_tag = (
                f'<image id="icon_{label_clean}" x="{x1:.1f}" y="{y1:.1f}" '
                f'width="{w:.1f}" height="{h:.1f}" '
                f'href="data:image/png;base64,{icon_b64}" '
                f'preserveAspectRatio="xMidYMid meet"/>'
            )
            svg_content = svg_content.replace("</svg>", f"  {image_tag}\n</svg>")
            if state.get("verbose"):
                logger.debug("    %s: FALLBACK — appended at (%.1f, %.1f, %.1fx%.1f)", label_clean, x1, y1, w, h)

    final_path = str(Path(run_dir) / "final.svg")
    Path(final_path).write_text(svg_content, encoding="utf-8")

    if state.get("verbose"):
        n_b64 = count_base64_images(svg_content)
        logger.info("  Final SVG saved with icons (%d chars, %d base64 images)", len(svg_content), n_b64)

    return {
        "svg_code": svg_content,
        "final_svg_path": final_path,
        "scale_factors": (scale_x, scale_y),
    }


def svg_render_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Render final SVG to PNG for review agents."""
    svg_path = state.get("final_svg_path", "")
    run_dir = state.get("run_dir", "")
    svg_code = state.get("svg_code", "")

    if not svg_path or not Path(svg_path).exists():
        return {}

    check_result = (
        run_post_render_checks(
            svg_code,
            state.get("valid_boxes") or [],
            state.get("ocr_texts") or [],
        )
        if svg_code
        else {
            "issues": [],
            "summary": "",
            "counts": {},
            "has_critical": False,
            "blocking_issues": [],
            "has_blocking_overlap": False,
        }
    )

    iteration = state.get("team_iteration", 0)
    iter_png = str(Path(run_dir) / f"method_architecture_v{iteration}.png")
    canonical_png = str(Path(run_dir) / "method_architecture.png")
    result = svg_to_png(svg_path, iter_png)
    if result:
        shutil.copy2(iter_png, canonical_png)
    png_path = canonical_png

    if result:
        if state.get("verbose"):
            logger.info("  SVG rendered to PNG: %s", png_path)
            if check_result.get("issues"):
                logger.info("  %s", check_result["summary"])
        return {
            "figure_path": png_path,
            "post_render_checks": check_result,
            "post_render_summary": check_result.get("summary", ""),
            "post_render_has_critical": check_result.get("has_critical", False),
            "post_render_has_blocking_overlap": check_result.get("has_blocking_overlap", False),
        }
    else:
        if state.get("verbose"):
            logger.warning("  SVG→PNG rendering failed (cairosvg/svglib not available)")
        # Fall back: use the generated image for review
        return {
            "figure_path": state.get("generated_image_path", ""),
            "post_render_checks": check_result,
            "post_render_summary": check_result.get("summary", ""),
            "post_render_has_critical": check_result.get("has_critical", False),
            "post_render_has_blocking_overlap": check_result.get("has_blocking_overlap", False),
        }


def _run_review_node(
    state: SVGMethodPipelineState,
    role: str,
    system_prompt_file: str,
    user_prompt_file: str,
    feedback_key: str,
    skip_fallback_extra: dict,
    error_fallback_extra: dict,
) -> dict:
    """Shared review logic for architect and advocate review nodes.

    *role* is used for logging and composite filename.  *feedback_key* is the
    state key to store the result under (e.g. ``"architect_feedback"``).
    *skip_fallback_extra* / *error_fallback_extra* carry role-specific fields
    for the fallback dicts (e.g. ``primary_issue`` vs ``severity``).
    """
    ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    figure_path = state.get("figure_path", "")
    if not figure_path or not Path(figure_path).exists():
        if state.get("verbose"):
            logger.warning("%s review skipped — no rendered image", role.capitalize())
        fallback = {
            "overall_score": 0.0,
            "dimensions": {},
            "verdict": "REVIEW_SKIPPED",
            "review_skipped": True,
            **skip_fallback_extra,
        }
        return {feedback_key: fallback}

    method_description = state.get("method_description", "")
    doc_type = state.get("doc_type", "journal")
    threshold = QUALITY_THRESHOLDS.get(doc_type.lower(), QUALITY_THRESHOLDS["default"])
    iteration = state.get("team_iteration", 0)
    max_iterations = state.get("max_team_iterations", 3)

    system_prompt = _load_prompt(system_prompt_file)
    user_prompt = _load_prompt(
        user_prompt_file,
        method_description=method_description[:3000],
        doc_type=doc_type,
        threshold=str(threshold),
        iteration=str(iteration + 1),
        max_iterations=str(max_iterations),
    )

    if state.get("verbose"):
        from llm import get_backend, get_model_display

        logger.info(
            "%s reviewing (iteration %d/%d, backend=%s, model=%s)...",
            role.capitalize(),
            iteration + 1,
            max_iterations,
            get_backend(),
            get_model_display("chat"),
        )

    import time as _time

    # Build composite image: original figure (LEFT) + SVG rendering (RIGHT)
    original_path = state.get("generated_image_path", "")
    comp_tag = "arch" if role == "architect" else "adv"
    if original_path and Path(original_path).exists():
        with Image.open(original_path) as _orig:
            orig_img = _orig.copy()
        with Image.open(figure_path) as _render:
            render_img = _render.copy()
        composite = build_composite_image(orig_img, render_img)
        comp_path = Path(state.get("run_dir", "")) / f"review_composite_{comp_tag}_v{iteration + 1}.png"
        composite.save(str(comp_path))
        image_url = encode_image_to_data_url(str(comp_path))
        orig_img.close()
        render_img.close()
        composite.close()
    else:
        image_url = encode_image_to_data_url(figure_path)

    # Append SVG code to user prompt for code-level feedback
    svg_code = state.get("svg_code", "")
    if svg_code:
        svg_for_review = _strip_base64_from_svg(svg_code)
        user_prompt += f"\n\n## Current SVG Code\n```xml\n{svg_for_review}\n```\n"
        user_prompt += "\nWhen reporting issues, reference specific SVG elements (tag names, id attributes, coordinates) in the `code_comments` field so the refinement agent can make precise edits."

    post_render_summary = state.get("post_render_summary", "")
    if post_render_summary:
        user_prompt += (
            "\n\n## Automated Post-Render Checks\n"
            f"{post_render_summary}\n"
            "Treat these as concrete layout defects to verify visually, especially text overlap, clipping, and element crowding."
        )

    if state.get("verbose"):
        _save_node_prompt(
            state.get("run_dir", ""), f"{role}_review", system_prompt, user_prompt, suffix=f"v{iteration + 1}"
        )

    response = None
    for _attempt in range(3):
        try:
            response = run_prompt(
                "chat",
                user_prompt.strip(),
                system_prompt=system_prompt.rstrip(),
                image_base64=image_url,
            ).strip()
            break
        except Exception as e:
            if _attempt < 2:
                _time.sleep(10 * (_attempt + 1))
            else:
                err_fallback = {
                    "overall_score": 0.0,
                    "dimensions": {},
                    "code_comments": [],
                    "verdict": "REVIEW_SKIPPED",
                    "review_skipped": True,
                    **error_fallback_extra,
                }
                if role == "architect":
                    err_fallback["improvement_instructions"] = f"Review failed: {e}"
                else:
                    err_fallback["key_critique"] = f"Review failed: {e}"
                return {feedback_key: err_fallback}

    feedback = _parse_review_json(response, role)
    feedback = dict(feedback)
    feedback["_meta"] = {
        "reviewed_figure_path": figure_path,
        "team_iteration": iteration + 1,
    }

    if state.get("verbose"):
        logger.info("  %s score: %s/12", role.capitalize(), feedback.get("overall_score", 0))

    run_dir = state.get("run_dir", "")
    if run_dir:
        review_path = Path(run_dir) / f"{role}_review_v{iteration + 1}.json"
        review_path.write_text(json.dumps(feedback, indent=2), encoding="utf-8")

    return {feedback_key: feedback}


def architect_review_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Technical Architect agent reviews the rendered diagram."""
    return _run_review_node(
        state,
        role="architect",
        system_prompt_file="architect_review_system.md",
        user_prompt_file="architect_review_user_template.md",
        feedback_key="architect_feedback",
        skip_fallback_extra={
            "primary_issue": "no_image",
            "improvement_instructions": "No rendered image available for review.",
        },
        error_fallback_extra={
            "primary_issue": "review_error",
        },
    )


def advocate_review_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Devil's Advocate agent reviews the rendered diagram.

    Also handles routing state (team_iteration, review_history) that was
    previously managed by the removed consensus_router node.
    """
    result = _run_review_node(
        state,
        role="advocate",
        system_prompt_file="advocate_review_system.md",
        user_prompt_file="advocate_review_user_template.md",
        feedback_key="advocate_feedback",
        skip_fallback_extra={
            "severity": "minor",
            "key_critique": "No rendered image available for review.",
            "counter_suggestions": [],
        },
        error_fallback_extra={
            "severity": "moderate",
            "counter_suggestions": [],
        },
    )

    # Update routing state (replaces consensus_router bookkeeping)
    adv_feedback = result.get("advocate_feedback", {})
    adv_score = adv_feedback.get("overall_score", 0)
    iteration = state.get("team_iteration", 0)
    max_iter = state.get("max_team_iterations", 3)
    doc_type = state.get("doc_type", "journal")
    threshold = QUALITY_THRESHOLDS.get(doc_type.lower(), QUALITY_THRESHOLDS["default"])

    # Pre-compute action for review_history (mirrors _route_after_advocate logic)
    review_unreliable = adv_feedback.get("review_skipped") or adv_feedback.get("parse_failure")
    if review_unreliable and iteration + 1 < max_iter:
        action = "refine"
    elif adv_score >= threshold or iteration + 1 >= max_iter:
        action = "accept"
    else:
        adv_dims = adv_feedback.get("dimensions", {})
        layout_flow = adv_dims.get("layout_flow", {}).get("score", 3.0)
        fidelity = adv_dims.get("fidelity_to_original", {}).get("score", 3.0)
        if layout_flow < 1.0 and fidelity < 1.0:
            action = "regenerate"
        else:
            action = "refine"

    review_entry = {
        "iteration": iteration + 1,
        "advocate_score": adv_score,
        "combined_score": adv_score,
        "action": action,
    }
    review_history = list(state.get("review_history") or [])
    review_history.append(review_entry)

    result["team_iteration"] = iteration + 1
    result["review_history"] = review_history
    result["combined_score"] = adv_score
    result["refinement_action"] = action

    # On max-iter accept, restore best prior SVG if current is worse
    if action == "accept" and iteration + 1 >= max_iter and adv_score < threshold:
        best_entry = max(review_history, key=lambda e: e.get("combined_score", 0))
        best_score = best_entry.get("combined_score", 0)
        best_iter = best_entry.get("iteration", iteration + 1)
        if best_score > adv_score:
            run_dir = state.get("run_dir", "")
            if run_dir:
                run_path = Path(run_dir)
                for candidate_name in [
                    f"refined_v{best_iter}.svg",
                    f"opt_valid_iter{best_iter - 1}.svg" if best_iter > 0 else None,
                ]:
                    if candidate_name:
                        candidate_path = run_path / candidate_name
                        if candidate_path.exists():
                            best_svg = candidate_path.read_text(encoding="utf-8")
                            (run_path / "final.svg").write_text(best_svg, encoding="utf-8")
                            result["svg_code"] = best_svg
                            result["final_svg_path"] = str(run_path / "final.svg")
                            best_png = str(run_path / "method_architecture.png")
                            svg_to_png(str(candidate_path), best_png)
                            result["figure_path"] = best_png
                            result["combined_score"] = best_score
                            if state.get("verbose"):
                                logger.info(
                                    "  Restored best SVG from iteration %d (score %.1f vs current %.1f)",
                                    best_iter,
                                    best_score,
                                    adv_score,
                                )
                            break

    post_render_checks = state.get("post_render_checks") or {}
    if post_render_checks.get("has_blocking_overlap") and iteration + 1 >= max_iter:
        top_messages = [issue.get("message", "") for issue in post_render_checks.get("blocking_issues", [])[:3]]
        message = "; ".join(msg for msg in top_messages if msg)
        result["error"] = f"Critical overlap issues remained after {max_iter} refinement iterations" + (
            f": {message}" if message else "."
        )
    elif _feedback_mentions_overlap(adv_feedback) and iteration + 1 >= max_iter:
        result["error"] = (
            f"Reviewer still reports overlap/clipping issues after {max_iter} refinement iterations: "
            f"{adv_feedback.get('key_critique', 'layout defects remain')}"
        )

    if state.get("verbose"):
        logger.info(
            "  Review: adv=%.1f/12 (threshold: %s, iter %d/%d) → %s",
            adv_score,
            threshold,
            iteration + 1,
            max_iter,
            action,
        )

    return result


def consensus_router_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Deterministic consensus routing based on combined agent scores.

    Routes to:
    - "accept": combined average >= threshold, or max iterations reached
    - "regenerate": fundamental structural issues (re-run image gen)
    - "refine": layout/style issues, or review was skipped/parse-failed (LLM edits SVG)
    """
    arch_feedback = state.get("architect_feedback", {})
    adv_feedback = state.get("advocate_feedback", {})

    arch_score = arch_feedback.get("overall_score", 0)
    adv_score = adv_feedback.get("overall_score", 0)
    combined = (arch_score + adv_score) / 2

    iteration = state.get("team_iteration", 0)
    max_iter = state.get("max_team_iterations", 3)
    doc_type = state.get("doc_type", "journal")
    threshold = QUALITY_THRESHOLDS.get(doc_type.lower(), QUALITY_THRESHOLDS["default"])

    combined_issues = _merge_issues(arch_feedback, adv_feedback)

    review_entry = {
        "iteration": iteration + 1,
        "architect_score": arch_score,
        "advocate_score": adv_score,
        "combined_score": combined,
        "action": "",
    }
    review_history = list(state.get("review_history") or [])

    if state.get("verbose"):
        logger.info(
            "  Consensus: arch=%.1f adv=%.1f combined=%.1f/12 (threshold: %s, iter %d/%d)",
            arch_score,
            adv_score,
            combined,
            threshold,
            iteration + 1,
            max_iter,
        )
        # Show dimension breakdown
        for dim_name, dim_data in arch_feedback.get("dimensions", {}).items():
            dim_score = dim_data.get("score", "?")
            dim_issues = dim_data.get("issues", [])
            issue_str = " — %s" % "; ".join(dim_issues[:2]) if dim_issues else ""
            logger.debug("    arch/%s: %s%s", dim_name, dim_score, issue_str)
        for dim_name, dim_data in adv_feedback.get("dimensions", {}).items():
            dim_score = dim_data.get("score", "?")
            dim_issues = dim_data.get("issues", [])
            issue_str = " — %s" % "; ".join(dim_issues[:2]) if dim_issues else ""
            logger.debug("    adv/%s: %s%s", dim_name, dim_score, issue_str)
        if combined_issues:
            logger.debug("  Top issues (%d total):", len(combined_issues))
            for iss in combined_issues[:5]:
                logger.debug("    [%s] %s/%s: %s", iss["severity"], iss["source"], iss["dimension"], iss["issue"][:100])

    # If either review was skipped or failed to parse, force refinement
    # regardless of score — fabricated scores must not drive acceptance.
    review_unreliable = (
        arch_feedback.get("review_skipped")
        or arch_feedback.get("parse_failure")
        or adv_feedback.get("review_skipped")
        or adv_feedback.get("parse_failure")
    )

    if review_unreliable and iteration + 1 < max_iter:
        action = "refine"
        if state.get("verbose"):
            logger.info("  Review skipped or parse failure — forcing refine")
    elif combined >= threshold or iteration + 1 >= max_iter:
        action = "accept"
        if review_unreliable:
            review_entry["unreviewed_final"] = True
        if iteration + 1 >= max_iter and combined < threshold:
            # Find best-scoring iteration and restore its SVG if it was better
            best_entry = max(review_history + [review_entry], key=lambda e: e.get("combined_score", 0))
            best_score = best_entry.get("combined_score", 0)
            best_iter = best_entry.get("iteration", iteration + 1)
            if best_score > combined:
                # Restore best iteration's SVG artifacts
                run_dir = state.get("run_dir", "")
                if run_dir:
                    run_path = Path(run_dir)
                    # Try refined SVG from that iteration, then optimized, then iter preview
                    for candidate_name in [
                        f"refined_v{best_iter}.svg",
                        f"opt_valid_iter{best_iter - 1}.svg" if best_iter > 0 else None,
                    ]:
                        if candidate_name:
                            candidate_path = run_path / candidate_name
                            if candidate_path.exists():
                                best_svg = candidate_path.read_text(encoding="utf-8")
                                # Write as final.svg so finalize picks it up
                                (run_path / "final.svg").write_text(best_svg, encoding="utf-8")
                                # Also update state for downstream nodes
                                review_entry["_restored_best"] = True
                                review_entry["_best_iter"] = best_iter
                                review_entry["_best_score"] = best_score
                                if state.get("verbose"):
                                    logger.info(
                                        "  Restored best SVG from iteration %d (score %.1f vs current %.1f)",
                                        best_iter,
                                        best_score,
                                        combined,
                                    )
                                break
            if state.get("verbose"):
                qualifier = " (UNREVIEWED)" if review_unreliable else ""
                logger.info(
                    "  Max iterations reached — accepting best effort (%.1f/12)%s", max(combined, best_score), qualifier
                )
    else:
        arch_dims = arch_feedback.get("dimensions", {})
        structural = arch_dims.get("structural_accuracy", {}).get("score", 2.0)
        completeness = arch_dims.get("completeness", {}).get("score", 2.0)

        if structural < 1.0 or completeness < 1.0:
            action = "regenerate"
        else:
            action = "refine"

    review_entry["action"] = action
    review_history.append(review_entry)

    if state.get("verbose"):
        logger.info("  Decision: %s", action)

    result = {
        "refinement_action": action,
        "combined_score": combined,
        "combined_issues": combined_issues,
        "review_history": review_history,
        "team_iteration": iteration + 1,
    }

    # If we restored a better iteration's SVG, update state so finalize uses it
    if review_entry.get("_restored_best"):
        run_dir = state.get("run_dir", "")
        if run_dir:
            final_path = str(Path(run_dir) / "final.svg")
            best_svg = Path(final_path).read_text(encoding="utf-8")
            result["svg_code"] = best_svg
            result["final_svg_path"] = final_path
            result["combined_score"] = review_entry["_best_score"]
            # Re-render the PNG for finalize
            best_png = str(Path(run_dir) / "method_architecture.png")
            svg_to_png(final_path, best_png)
            result["figure_path"] = best_png

    return result


def svg_refinement_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """LLM refines SVG based on advocate review feedback."""
    ensure_gpt()
    from llm import run_prompt, encode_image_to_data_url

    svg_code = state.get("svg_code", "")
    adv_feedback = state.get("advocate_feedback", {})

    if state.get("verbose"):
        from llm import get_backend, get_model_display

        logger.info("Refining SVG (backend=%s, model=%s)...", get_backend(), get_model_display("chat"))

    # Build feedback text from advocate only
    parts = []

    key_critique = adv_feedback.get("key_critique", "")
    if key_critique:
        parts.append(f"## Review Feedback\n{key_critique}")

    suggestions = adv_feedback.get("counter_suggestions", [])
    if suggestions:
        parts.append("Suggestions:\n" + "\n".join(f"- {s}" for s in suggestions))

    for dim_name, dim_data in adv_feedback.get("dimensions", {}).items():
        issues = dim_data.get("issues", [])
        if issues:
            parts.append(f"- {dim_name}: " + "; ".join(issues))

    # Append code-level fix instructions from advocate
    adv_code_comments = adv_feedback.get("code_comments", [])
    if adv_code_comments:
        parts.append("\n## Code-Level Fix Instructions")
        parts.append("Apply these specific SVG edits:")
        for c in adv_code_comments:
            parts.append(f"- {c}")

    current_check = state.get("post_render_checks")
    if not current_check:
        current_check = run_post_render_checks(
            svg_code,
            state.get("valid_boxes") or [],
            state.get("ocr_texts") or [],
        )
    auto_fix_instructions = build_automated_refinement_instructions(current_check)
    if current_check.get("summary"):
        parts.append("\n## Automated Layout Checks")
        parts.append(current_check["summary"])
    if auto_fix_instructions:
        parts.append("\n## Deterministic Fix Instructions")
        parts.append("Resolve these concrete geometry issues before making secondary aesthetic edits:")
        for instruction in auto_fix_instructions:
            parts.append(f"- {instruction}")

    feedback_text = "\n".join(parts)

    system_prompt = _load_prompt("svg_refinement_system.md")

    # Build composite: original + samed (if boxes) + current SVG render
    run_dir_ref = state.get("run_dir", "")
    image_url = None
    original_path = state.get("generated_image_path", "")
    samed_path = state.get("samed_image_path", "")
    rendered_path = state.get("figure_path", "")

    valid_boxes = state.get("valid_boxes") or []
    icon_infos_ref = state.get("icon_infos") or []
    if original_path and Path(original_path).exists() and rendered_path and Path(rendered_path).exists():
        with Image.open(original_path) as _o:
            orig_img = _o.copy()
        with Image.open(rendered_path) as _r:
            render_img = _r.copy()

        if icon_infos_ref and samed_path and Path(samed_path).exists():
            with Image.open(samed_path) as _s:
                sam_img = _s.copy()
            top = build_composite_image(orig_img, sam_img)
            full = build_composite_image(top, render_img)
            sam_img.close()
            top.close()
            composite_desc = (
                "The attached image shows three views side by side:\n"
                "- LEFT: original figure (target)\n"
                "- MIDDLE: annotated figure with icon regions marked\n"
                "- RIGHT: current SVG rendering\n"
            )
        else:
            full = build_composite_image(orig_img, render_img)
            composite_desc = (
                "The attached image shows two views side by side:\n"
                "- LEFT: original figure (target)\n"
                "- RIGHT: current SVG rendering\n"
            )

        if run_dir_ref:
            comp_path = Path(run_dir_ref) / "composite_for_refinement.png"
            full.save(str(comp_path))
            image_url = encode_image_to_data_url(str(comp_path))
        orig_img.close()
        render_img.close()
        full.close()
    else:
        # Fallback: single rendered image
        for key in ("figure_path", "generated_image_path"):
            path = state.get(key, "")
            if path and Path(path).exists():
                image_url = encode_image_to_data_url(path)
                break
        composite_desc = "The attached image shows the current rendered diagram.\n"

    # Count base64 images to validate after refinement
    original_count = count_base64_images(svg_code)

    # Strip base64 from SVG for LLM, provide box + OCR context
    svg_for_llm = _strip_base64_from_svg(svg_code)
    ocr_texts = state.get("ocr_texts") or []
    box_context = _build_box_context(valid_boxes, icon_infos_ref)
    ocr_context = _build_ocr_context(ocr_texts)

    ref_parts = [
        composite_desc,
        f"Current SVG code (base64 image data stripped for brevity):\n```xml\n{svg_for_llm}\n```",
        "",
        box_context,
    ]
    if ocr_context:
        ref_parts.append("")
        ref_parts.append(ocr_context)
    ref_parts.extend(
        [
            "",
            f"## Agent Feedback\n{feedback_text}",
            "",
            "CRITICAL INSTRUCTIONS:",
            "1. Refine the SVG to address ALL feedback points.",
            "2. Output ONLY the refined SVG code for a SINGLE unified diagram.",
            "3. STRUCTURAL regions: improve colors, shapes, positions to match the original figure.",
            '4. ICON placeholder regions (<g id="AFxx"> or <image id="icon_AFxx">): keep exactly as-is. Do NOT remove or modify.',
            '5. Where the SVG has href="...BASE64_DATA_STRIPPED...", preserve that <image> tag exactly as-is.',
        ]
    )
    user_text = "\n".join(ref_parts)

    if state.get("verbose"):
        iteration = state.get("team_iteration", 0)
        _save_node_prompt(state.get("run_dir", ""), "svg_refinement", system_prompt, user_text, suffix=f"v{iteration}")

    for attempt in range(3):
        try:
            response = run_prompt(
                "chat",
                user_text,
                system_prompt=system_prompt,
                image_base64=image_url,
            ).strip()
            break
        except Exception as e:
            if attempt < 2:
                _time.sleep(10 * (attempt + 1))
            else:
                if state.get("verbose"):
                    logger.error("  Refinement failed: %s", e)
                return {}

    # Save raw refinement response
    run_dir = state.get("run_dir", "")
    iteration = state.get("team_iteration", 0)
    if state.get("verbose") and run_dir:
        raw_path = Path(run_dir) / f"refinement_raw_response_v{iteration}.txt"
        raw_path.write_text(response, encoding="utf-8")
        logger.debug("  Raw refinement response saved (%d chars) → %s", len(response), raw_path.name)

    refined_svg = extract_svg_code(response)
    if not refined_svg:
        if state.get("verbose"):
            logger.warning("  Could not extract SVG from refinement response")
        return {}

    # Restore base64 data that was stripped for the LLM
    if original_count > 0:
        refined_svg = _restore_base64_in_svg(refined_svg, svg_code)
        if state.get("verbose"):
            restored_count = count_base64_images(refined_svg)
            logger.debug("  Restored base64 images: %d/%d", restored_count, original_count)

    # Validate base64 images survived
    if original_count > 0:
        valid, msg = validate_base64_images(refined_svg, original_count)
        if not valid:
            if state.get("verbose"):
                logger.warning("  Rejecting refinement: %s", msg)
                rejected_path = Path(run_dir) / f"refinement_rejected_v{iteration}.svg"
                rejected_path.write_text(refined_svg, encoding="utf-8")
                logger.debug("  Rejected SVG saved → %s", rejected_path.name)
            return {}

    # Regression guard: compare post-render checks before/after refinement
    valid_boxes = state.get("valid_boxes") or []
    ocr_texts = state.get("ocr_texts") or []
    old_check = run_post_render_checks(svg_code, valid_boxes, ocr_texts)
    new_check = run_post_render_checks(refined_svg, valid_boxes, ocr_texts)
    old_issues = len(old_check.get("issues", []))
    new_issues = len(new_check.get("issues", []))

    if new_issues > old_issues and new_check.get("has_critical"):
        if state.get("verbose"):
            logger.warning(
                "  Regression guard: refinement worsened issues (%d → %d) — keeping original SVG",
                old_issues,
                new_issues,
            )
            if run_dir:
                rejected_path = Path(run_dir) / f"refinement_regressed_v{iteration}.svg"
                rejected_path.write_text(refined_svg, encoding="utf-8")
                logger.debug("  Regressed SVG saved → %s", rejected_path.name)
        return {}

    # Update final SVG path
    if run_dir:
        refined_path = str(Path(run_dir) / f"refined_v{iteration}.svg")
        Path(refined_path).write_text(refined_svg, encoding="utf-8")
        final_path = str(Path(run_dir) / "final.svg")
        Path(final_path).write_text(refined_svg, encoding="utf-8")

    if state.get("verbose"):
        n_b64 = count_base64_images(refined_svg)
        check_delta = new_issues - old_issues
        delta_str = f", checks {old_issues}→{new_issues} ({'+' if check_delta > 0 else ''}{check_delta})"
        logger.info("  SVG refined (%d chars, %d base64 images%s)", len(refined_svg), n_b64, delta_str)
        # Render refined preview
        if run_dir:
            refined_png = str(Path(run_dir) / f"refined_preview_v{iteration}.png")
            if svg_to_png(str(Path(run_dir) / f"refined_v{iteration}.svg"), refined_png):
                logger.debug("  Refined preview → refined_preview_v%d.png", iteration)

    return {
        "svg_code": refined_svg,
        "final_svg_path": final_path if run_dir else "",
    }


def regenerate_prompt_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Build a refined prompt for image regeneration based on agent feedback."""
    method_description = state.get("method_description", "")
    drawing_instruction = _extract_drawing_instruction(method_description)

    arch_feedback = state.get("architecture_review_feedback", {})
    adv_feedback = state.get("advocate_feedback", {})
    iteration = state.get("team_iteration", 0)
    arch_iteration = state.get("architecture_review_iteration", 0)

    improvements = []
    if arch_feedback and iteration == 0:
        summary = arch_feedback.get("feedback", "")
        if summary:
            improvements.append(summary)
        improvements.extend(
            f"Add or restore missing component: {item}" for item in arch_feedback.get("missing_components", []) if item
        )
        improvements.extend(f"Fix architecture issue: {item}" for item in arch_feedback.get("issues", []) if item)
    else:
        key_critique = adv_feedback.get("key_critique", "")
        if key_critique:
            improvements.append(key_critique)
        improvements.extend(adv_feedback.get("counter_suggestions", []))

    improvement_text = "\n".join(f"- {imp}" for imp in improvements if imp)

    refined_prompt = (
        f"{SCIENTIFIC_DIAGRAM_GUIDELINES}\n\n"
        f"ARCHITECTURE DIAGRAM REQUEST:\n\n{drawing_instruction}\n\n"
        f"ITERATION {iteration + 1}: The previous version had these issues. "
        f"Generate an improved version:\n{improvement_text}\n\n"
        f"Generate a cleaner, more accurate diagram."
    )

    if state.get("verbose"):
        logger.info("Built regeneration prompt (%d chars)", len(refined_prompt))

    result = {
        "refined_prompt": refined_prompt,
        # Clear all SAM-derived state to prevent stale data on regeneration
        "sam_stage1_prompts": [],
        "sam_stage2_prompts": [],
        "sam_stage1_results": [],
        "sam_stage2_results": [],
        "sam_stage1_overlay_path": "",
        "sam_agent_classified": False,
        "valid_boxes": [],
    }
    if arch_feedback and iteration == 0:
        result["architecture_review_iteration"] = arch_iteration + 1
    return result


def finalize_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Save final artifacts: SVG, PNG, review log."""
    run_dir = state.get("run_dir", "")
    if not run_dir:
        return {"success": True}

    run_path = Path(run_dir)

    # Copy final SVG to canonical name
    final_svg = state.get("final_svg_path", "")
    canonical_svg = run_path / "method_architecture.svg"
    if final_svg and Path(final_svg).exists():
        if Path(final_svg) != canonical_svg:
            shutil.copy2(final_svg, canonical_svg)

    # Ensure PNG exists
    figure_path = state.get("figure_path", "")
    canonical_png = run_path / "method_architecture.png"
    if figure_path and Path(figure_path).exists():
        if Path(figure_path) != canonical_png:
            shutil.copy2(figure_path, canonical_png)

    # Save review log
    review_log = {
        "svg_fix_iterations": state.get("svg_fix_iteration", 0),
        "optimize_iterations": state.get("optimize_iterations", 0),
        "team_iterations": state.get("team_iteration", 0),
        "final_score": state.get("combined_score", 0),
        "review_history": state.get("review_history", []),
        "icon_count": len(state.get("icon_infos") or []),
        "box_count": len(state.get("valid_boxes") or []),
    }
    review_log_path = run_path / "review_log.json"
    review_log_path.write_text(json.dumps(review_log, indent=2), encoding="utf-8")

    figure_paths = list(state.get("figure_paths") or [])
    if str(canonical_svg) not in figure_paths:
        figure_paths.append(str(canonical_svg))
    if str(canonical_png) not in figure_paths:
        figure_paths.append(str(canonical_png))

    if state.get("verbose"):
        logger.info("Finalized: %s", canonical_svg)
        logger.info("  PNG: %s", canonical_png)
        logger.info("  Review log: %s", review_log_path)

    return {
        "figure_path": str(canonical_png),
        "figure_paths": figure_paths,
        "success": True,
    }


def fail_end_node(state: SVGMethodPipelineState) -> SVGMethodPipelineState:
    """Handle pipeline failures gracefully."""
    run_dir = state.get("run_dir", "")
    svg_code = state.get("svg_code", "")
    svg_errors = state.get("svg_errors", [])

    if run_dir and svg_code:
        run_path = Path(run_dir)
        (run_path / "method_architecture_FAILED.svg").write_text(svg_code, encoding="utf-8")
        (run_path / "validation_errors.json").write_text(
            json.dumps({"errors": svg_errors, "svg_fix_iterations": state.get("svg_fix_iteration", 0)}, indent=2),
            encoding="utf-8",
        )

    # Preserve the original error if it was set by an earlier node (e.g. image generation)
    prior_error = state.get("error", "")
    if prior_error:
        return {"success": False, "error": prior_error}

    return {
        "success": False,
        "error": f"SVG validation failed after {state.get('svg_fix_iteration', 0)} fix attempts. "
        f"Errors: {'; '.join(svg_errors[:3])}",
    }


# ── Routing functions ─────────────────────────────────────────────────


def _route_after_image_gen(state: SVGMethodPipelineState) -> str:
    if state.get("error"):
        return "fail_end"
    if not state.get("generated_image_path"):
        return "fail_end"
    return "architecture_review"


def _route_after_architecture_review(state: SVGMethodPipelineState) -> str:
    if state.get("error"):
        return "fail_end"
    review = state.get("architecture_review_feedback", {})
    if review.get("pass", True):
        return "sam3_detect"
    if state.get("architecture_review_iteration", 0) < 2:
        return "regenerate_prompt"
    return "sam3_detect"


def _route_after_svg_validation(state: SVGMethodPipelineState) -> str:
    if state.get("svg_valid"):
        return "icon_replacement"
    iteration = state.get("svg_fix_iteration", 0)
    max_iter = state.get("max_svg_fix_iterations", 3)
    if iteration >= max_iter:
        return "fail_end"
    return "svg_fix"


def _route_after_consensus(state: SVGMethodPipelineState) -> str:
    """Legacy routing — kept for backwards compatibility."""
    action = state.get("refinement_action", "accept")
    if action == "accept":
        return "finalize"
    elif action == "regenerate":
        return "regenerate_prompt"
    else:  # refine
        return "svg_refinement"


def _feedback_mentions_overlap(feedback: dict) -> bool:
    """Return True when review feedback still calls out overlap or clipping problems."""
    if not feedback:
        return False
    keywords = (
        "overlap",
        "intersect",
        "clip",
        "clipp",
        "cramp",
        "crowd",
        "collid",
        "too close",
        "boundary",
        "edge",
    )
    texts = []
    if feedback.get("key_critique"):
        texts.append(str(feedback.get("key_critique")))
    texts.extend(str(item) for item in feedback.get("counter_suggestions", []) if item)
    texts.extend(str(item) for item in feedback.get("code_comments", []) if item)
    for dim_data in feedback.get("dimensions", {}).values():
        texts.extend(str(item) for item in dim_data.get("issues", []) if item)
    haystack = "\n".join(texts).lower()
    return any(keyword in haystack for keyword in keywords)


def _route_after_advocate(state: SVGMethodPipelineState) -> str:
    """Route based on advocate review score alone (architect removed).

    Note: advocate_review_node already incremented team_iteration before
    this router runs, so we compare iteration directly against max_iter.
    """
    if state.get("error"):
        return "fail_end"

    adv_feedback = state.get("advocate_feedback", {})
    adv_score = adv_feedback.get("overall_score", 0)

    iteration = state.get("team_iteration", 0)  # already incremented by advocate node
    max_iter = state.get("max_team_iterations", 3)
    doc_type = state.get("doc_type", "journal")
    threshold = QUALITY_THRESHOLDS.get(doc_type.lower(), QUALITY_THRESHOLDS["default"])

    review_unreliable = adv_feedback.get("review_skipped") or adv_feedback.get("parse_failure")
    blocking_overlap = bool(state.get("post_render_has_blocking_overlap"))
    reviewer_overlap = _feedback_mentions_overlap(adv_feedback)

    if review_unreliable and iteration < max_iter:
        return "svg_refinement"
    if blocking_overlap and iteration < max_iter:
        return "svg_refinement"
    if blocking_overlap and iteration >= max_iter:
        return "fail_end"
    if reviewer_overlap and iteration < max_iter:
        return "svg_refinement"
    if reviewer_overlap and iteration >= max_iter:
        return "fail_end"
    elif adv_score >= threshold or iteration >= max_iter:
        return "finalize"
    else:
        # Check for fundamental structural issues from advocate
        adv_dims = adv_feedback.get("dimensions", {})
        layout_flow = adv_dims.get("layout_flow", {}).get("score", 3.0)
        fidelity = adv_dims.get("fidelity_to_original", {}).get("score", 3.0)
        if layout_flow < 1.0 and fidelity < 1.0:
            return "regenerate_prompt"
        return "svg_refinement"


# ── Graph construction ────────────────────────────────────────────────


def build_svg_method_pipeline():
    """Build the SVG method drawing pipeline.

    Flow:
      load_markdown → method_data_explorer → method_proposer → image_generation → architecture_review
        → sam3_detect → sam3_review → sam3_merge_classify
        → ocr_text_detection → icon_extraction → visualization_code_gen
        → svg_generation → svg_validation
          → [valid: icon_replacement → svg_render → advocate_review]
          → [invalid: svg_fix → svg_validation (loop, max 3)]
          → [exhausted: fail_end → END]

      advocate_review routes:
        → accept → finalize → END
        → refine → svg_refinement → svg_validation → icon_replacement → ...
        → regenerate → regenerate_prompt → image_generation → ...

    Note: svg_optimization removed — it consistently over-compressed layouts
    and reverted refinement changes. Refinement handles quality improvement.
    """
    g = StateGraph(SVGMethodPipelineState)

    # Reused nodes from _method_shared
    g.add_node("load_markdown", load_markdown_node)
    g.add_node("method_data_explorer", method_data_explorer_node)
    g.add_node("method_proposer", method_proposer_node)

    # Image generation
    g.add_node("image_generation", image_generation_node)
    g.add_node("architecture_review", architecture_review_node)

    # SAM3 two-stage detection + OCR + icon extraction
    g.add_node("sam3_detect", sam3_detect_node)
    g.add_node("sam3_review", sam3_review_node)
    g.add_node("sam3_merge_classify", sam3_merge_classify_node)
    g.add_node("ocr_text_detection", ocr_text_detection_node)
    g.add_node("icon_extraction", icon_extraction_node)
    g.add_node("visualization_code_gen", visualization_code_gen_node)

    # SVG generation + validation
    g.add_node("svg_generation", svg_generation_node)
    g.add_node("svg_validation", svg_validation_node)
    g.add_node("svg_fix", svg_fix_node)

    # Icon replacement + render (svg_optimization removed)
    g.add_node("icon_replacement", icon_replacement_node)
    g.add_node("svg_render", svg_render_node)

    # Review (advocate only — architect removed)
    g.add_node("advocate_review", advocate_review_node)

    # Refinement paths
    g.add_node("svg_refinement", svg_refinement_node)
    g.add_node("regenerate_prompt", regenerate_prompt_node)

    # Terminal
    g.add_node("finalize", finalize_node)
    g.add_node("fail_end", fail_end_node)

    # Linear flow
    g.set_entry_point("load_markdown")
    g.add_edge("load_markdown", "method_data_explorer")
    g.add_edge("method_data_explorer", "method_proposer")
    g.add_edge("method_proposer", "image_generation")

    # Conditional: after image generation
    g.add_conditional_edges(
        "image_generation",
        _route_after_image_gen,
        {
            "architecture_review": "architecture_review",
            "fail_end": "fail_end",
        },
    )

    g.add_conditional_edges(
        "architecture_review",
        _route_after_architecture_review,
        {
            "sam3_detect": "sam3_detect",
            "regenerate_prompt": "regenerate_prompt",
            "fail_end": "fail_end",
        },
    )

    # Linear: SAM3 two-stage → OCR → icon extraction → visualization icon codegen → SVG generation → validation
    g.add_edge("sam3_detect", "sam3_review")
    g.add_edge("sam3_review", "sam3_merge_classify")
    g.add_edge("sam3_merge_classify", "ocr_text_detection")
    g.add_edge("ocr_text_detection", "icon_extraction")
    g.add_edge("icon_extraction", "visualization_code_gen")
    g.add_edge("visualization_code_gen", "svg_generation")
    g.add_edge("svg_generation", "svg_validation")

    # Conditional: after SVG validation
    g.add_conditional_edges(
        "svg_validation",
        _route_after_svg_validation,
        {
            "icon_replacement": "icon_replacement",
            "svg_fix": "svg_fix",
            "fail_end": "fail_end",
        },
    )

    # Fix loops back to validation
    g.add_edge("svg_fix", "svg_validation")

    # Linear: icon replacement → render → advocate review
    g.add_edge("icon_replacement", "svg_render")
    g.add_edge("svg_render", "advocate_review")

    # Conditional: after advocate review (replaces consensus_router)
    g.add_conditional_edges(
        "advocate_review",
        _route_after_advocate,
        {
            "finalize": "finalize",
            "svg_refinement": "svg_refinement",
            "regenerate_prompt": "regenerate_prompt",
            "fail_end": "fail_end",
        },
    )

    # Refinement loops back to validation
    g.add_edge("svg_refinement", "svg_validation")

    # Regeneration loops back to image generation
    g.add_edge("regenerate_prompt", "image_generation")

    # Terminal edges
    g.add_edge("finalize", END)
    g.add_edge("fail_end", END)

    return g.compile()


app_svg_method = build_svg_method_pipeline()
