"""
SVG utility functions extracted from AutoFigure-Edit/autofigure2.py for reuse
in the SVG method drawing pipeline.

Provides:
- Box overlap / merge helpers for SAM3 detections
- Dynamic label font sizing
- SVG code extraction from LLM responses
- SVG syntax validation (lxml / stdlib fallback)
- SVG dimension parsing and coordinate scale factors
- SVG → PNG rendering (cairosvg / svglib fallback)
- Base64-image validation inside SVG code
- BEN2 service health check
- draw_samed_image for visualizing SAM3 box annotations
- Composite image builder (side-by-side) for multimodal LLM calls
"""
from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


# ── Pipeline config loader ──────────────────────────────────────────

import threading as _threading

_PIPELINE_CONFIG: dict | None = None
_CONFIG_LOCK = _threading.Lock()


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (non-destructive copy)."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_pipeline_config() -> dict:
    """Load configs/pipeline.yaml, caching the result. Falls back to empty dict.

    If the env var ``HAPPYFIGURE_CONFIG`` is set, that file is loaded and
    deep-merged on top of the base config — so you can override only the
    keys you care about without modifying the original pipeline.yaml.

    Thread-safe.  Gracefully handles missing PyYAML, malformed YAML, or
    read errors — returns ``{}`` with a printed warning.
    """
    global _PIPELINE_CONFIG
    if _PIPELINE_CONFIG is not None:
        return _PIPELINE_CONFIG
    with _CONFIG_LOCK:
        if _PIPELINE_CONFIG is not None:  # double-check after lock
            return _PIPELINE_CONFIG
        config_path = Path(__file__).resolve().parent.parent / "configs" / "pipeline.yaml"
        try:
            if config_path.exists():
                import yaml
                raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                _PIPELINE_CONFIG = raw if isinstance(raw, dict) else {}
            else:
                _PIPELINE_CONFIG = {}
        except Exception as exc:
            import warnings
            warnings.warn(f"Failed to load pipeline config ({exc}); using defaults", stacklevel=2)
            _PIPELINE_CONFIG = {}
        # Overlay user overrides from HAPPYFIGURE_CONFIG
        override_path = os.environ.get("HAPPYFIGURE_CONFIG", "")
        if override_path:
            try:
                import yaml
                override_raw = yaml.safe_load(Path(override_path).read_text(encoding="utf-8"))
                if isinstance(override_raw, dict):
                    _PIPELINE_CONFIG = _deep_merge(_PIPELINE_CONFIG, override_raw)
            except Exception as exc:
                import warnings
                warnings.warn(f"Failed to load HAPPYFIGURE_CONFIG={override_path} ({exc})", stacklevel=2)
    return _PIPELINE_CONFIG


PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _resolve_prompt_path(name: str) -> Path:
    """Find a prompt file by name, searching subdirectories.

    Supports both flat (``prompts/foo.md``) and organized
    (``prompts/figure/foo.md``) layouts for backward compatibility.
    """
    # Direct path first (caller may pass "figure/foo.md" explicitly)
    direct = PROMPT_DIR / name
    if direct.exists():
        return direct
    # Search known subdirectories
    for subdir in ("figure", "svg", "method", "shared"):
        candidate = PROMPT_DIR / subdir / name
        if candidate.exists():
            return candidate
    return direct  # return original (will fail gracefully)


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template from the ``prompts/`` directory tree.

    Searches ``prompts/`` and its subdirectories (``figure/``, ``svg/``,
    ``method/``, ``shared/``).  Auto-resolves ``{{palette_reference}}``
    and ``{{size_tiers}}`` from ``prompts/shared/``.
    """
    path = _resolve_prompt_path(name)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    # Auto-resolve shared placeholder files
    _auto_includes = {
        "{{palette_reference}}": "palette_reference.md",
        "{{size_tiers}}": "size_tiers.md",
    }
    for placeholder, include_file in _auto_includes.items():
        if placeholder in text:
            inc_path = _resolve_prompt_path(include_file)
            inc_content = inc_path.read_text(encoding="utf-8") if inc_path.exists() else ""
            text = text.replace(placeholder, inc_content)
    for k, v in (kwargs or {}).items():
        text = text.replace("{{" + k + "}}", v or "")
    return text


# ── Box overlap / merge ─────────────────────────────────────────────


def calculate_overlap_ratio(box1: dict, box2: dict) -> float:
    """Return intersection / smaller-box-area for two {x1, y1, x2, y2} dicts."""
    x1 = max(box1["x1"], box2["x1"])
    y1 = max(box1["y1"], box2["y1"])
    x2 = min(box1["x2"], box2["x2"])
    y2 = min(box1["y2"], box2["y2"])

    if x2 <= x1 or y2 <= y1:
        return 0.0

    intersection = (x2 - x1) * (y2 - y1)
    area1 = (box1["x2"] - box1["x1"]) * (box1["y2"] - box1["y1"])
    area2 = (box2["x2"] - box2["x1"]) * (box2["y2"] - box2["y1"])

    if area1 == 0 or area2 == 0:
        return 0.0

    return intersection / min(area1, area2)


def merge_two_boxes(box1: dict, box2: dict) -> dict:
    """Merge two boxes into their minimum bounding rectangle."""
    merged = {
        "x1": min(box1["x1"], box2["x1"]),
        "y1": min(box1["y1"], box2["y1"]),
        "x2": max(box1["x2"], box2["x2"]),
        "y2": max(box1["y2"], box2["y2"]),
        "score": max(box1.get("score", 0), box2.get("score", 0)),
    }
    prompt1 = box1.get("prompt", "")
    prompt2 = box2.get("prompt", "")
    if prompt1 and prompt2:
        if prompt1 == prompt2:
            merged["prompt"] = prompt1
        else:
            merged["prompt"] = prompt1 if box1.get("score", 0) >= box2.get("score", 0) else prompt2
    elif prompt1:
        merged["prompt"] = prompt1
    elif prompt2:
        merged["prompt"] = prompt2
    return merged


def merge_overlapping_boxes(boxes: list, overlap_threshold: float = 0.9) -> list:
    """Iteratively merge overlapping boxes, re-number with <AF>NN labels."""
    if overlap_threshold <= 0 or len(boxes) <= 1:
        return boxes

    working = [b.copy() for b in boxes]
    merged = True
    while merged:
        merged = False
        n = len(working)
        for i in range(n):
            if merged:
                break
            for j in range(i + 1, n):
                ratio = calculate_overlap_ratio(working[i], working[j])
                if ratio >= overlap_threshold:
                    new_box = merge_two_boxes(working[i], working[j])
                    working = [working[k] for k in range(n) if k != i and k != j]
                    working.append(new_box)
                    merged = True
                    break

    result = []
    for idx, box in enumerate(working):
        r = {
            "id": idx,
            "label": f"<AF>{idx + 1:02d}",
            "x1": box["x1"],
            "y1": box["y1"],
            "x2": box["x2"],
            "y2": box["y2"],
            "score": box.get("score", 0),
        }
        if "prompt" in box:
            r["prompt"] = box["prompt"]
        result.append(r)
    return result


# ── Font helper ──────────────────────────────────────────────────────


def get_label_font(box_width: int, box_height: int) -> Optional[ImageFont.FreeTypeFont]:
    """Return a bold font sized dynamically for the box."""
    cfg = load_pipeline_config().get("svg", {})
    font_min = cfg.get("label_font_min", 12)
    font_max = cfg.get("label_font_max", 48)
    min_dim = min(box_width, box_height)
    font_size = max(font_min, min(font_max, min_dim // 4))
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for fp in font_paths:
        try:
            return ImageFont.truetype(fp, font_size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default()
    except (OSError, IOError):
        return None


# ── SVG extraction ───────────────────────────────────────────────────


def extract_svg_code(content: str) -> Optional[str]:
    """Extract ``<svg>...</svg>`` from LLM response text."""
    match = re.search(r'(<svg[\s\S]*?</svg>)', content, re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.search(r'```(?:svg|xml)?\s*([\s\S]*?)```', content)
    if match:
        code = match.group(1).strip()
        if code.startswith('<svg'):
            return code

    if content.strip().startswith('<svg'):
        return content.strip()

    return None


# ── SVG validation ───────────────────────────────────────────────────


def validate_svg_syntax(svg_code: str) -> tuple[bool, list[str]]:
    """Validate SVG/XML syntax using lxml (preferred) or stdlib xml.etree."""
    try:
        from lxml import etree
        etree.fromstring(svg_code.encode('utf-8'))
        return True, []
    except ImportError:
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(svg_code)
            return True, []
        except ET.ParseError as e:
            return False, [f"XML parse error: {e!s}"]
    except Exception as e:
        try:
            from lxml import etree
            if isinstance(e, etree.XMLSyntaxError):
                errors = []
                for err in e.error_log:
                    errors.append(f"line {err.line}, col {err.column}: {err.message}")
                if not errors:
                    errors.append(f"line {e.lineno}, col {e.offset}: {e.msg}")
                return False, errors
        except (AttributeError, TypeError):
            pass  # Error log format varies between lxml/xml.etree
        return False, [f"Parse error: {e!s}"]


# ── SVG dimensions / scale ───────────────────────────────────────────


def get_svg_dimensions(svg_code: str) -> tuple[Optional[float], Optional[float]]:
    """Extract (width, height) from SVG viewBox or width/height attributes."""
    vb_match = re.search(r'viewBox=["\']([^"\']+)["\']', svg_code, re.IGNORECASE)
    if vb_match:
        parts = vb_match.group(1).strip().split()
        if len(parts) >= 4:
            try:
                return float(parts[2]), float(parts[3])
            except ValueError:
                pass

    def _parse_dim(attr: str) -> Optional[float]:
        m = re.search(rf'{attr}=["\']([^"\']+)["\']', svg_code, re.IGNORECASE)
        if m:
            nm = re.match(r'([\d.]+)', m.group(1).strip())
            if nm:
                try:
                    return float(nm.group(1))
                except ValueError:
                    pass
        return None

    w, h = _parse_dim('width'), _parse_dim('height')
    if w and h:
        return w, h
    return None, None


def calculate_scale_factors(
    figure_width: int, figure_height: int,
    svg_width: float, svg_height: float,
) -> tuple[float, float]:
    """Return (scale_x, scale_y) from figure pixel coords to SVG coords."""
    return svg_width / figure_width, svg_height / figure_height


# ── SVG → PNG ────────────────────────────────────────────────────────


def svg_to_png(svg_path: str, output_path: str, scale: float | None = None) -> Optional[str]:
    """Render SVG to PNG with a white background using cairosvg (preferred) or svglib."""
    if scale is None:
        scale = load_pipeline_config().get("svg", {}).get("render_scale", 2.0)
    try:
        import cairosvg
        # Render to bytes first, then flatten RGBA → RGB with white background
        png_data = cairosvg.svg2png(url=svg_path, scale=scale)
        with Image.open(io.BytesIO(png_data)) as img:
            rgb = _flatten_to_rgb(img)
            rgb.save(output_path)
        return output_path
    except ImportError:
        try:
            from svglib.svglib import svg2rlg
            from reportlab.graphics import renderPM
            drawing = svg2rlg(svg_path)
            renderPM.drawToFile(drawing, output_path, fmt="PNG")
            return output_path
        except (ImportError, Exception):
            return None
    except Exception:
        return None


# ── Base64-image validation ──────────────────────────────────────────


def count_base64_images(svg_code: str) -> int:
    """Count embedded base64 images in SVG code."""
    pattern = r'(?:href|xlink:href)=["\']data:image/[^;]+;base64,[A-Za-z0-9+/=]+'
    return len(re.findall(pattern, svg_code))


def validate_base64_images(svg_code: str, expected_count: int) -> tuple[bool, str]:
    """Validate that SVG contains the expected number of intact base64 images."""
    actual = count_base64_images(svg_code)
    if actual < expected_count:
        return False, f"base64 image count: expected {expected_count}, got {actual}"

    for m in re.finditer(r'data:image/[^;]+;base64,([A-Za-z0-9+/=]+)', svg_code):
        b64 = m.group(1)
        if len(b64) % 4 != 0:
            return False, f"Truncated base64 data (length {len(b64)} not multiple of 4)"
        if len(b64) < 100:
            return False, f"Suspiciously short base64 data (length {len(b64)})"

    return True, f"base64 validation passed: {actual} image(s)"


# ── SAMed image drawing ──────────────────────────────────────────────


def draw_samed_image(
    image: Image.Image,
    valid_boxes: list[dict],
    output_path: str,
) -> str:
    """Draw gray boxes + labels onto a copy of *image*, save to *output_path*."""
    samed = image.copy()
    draw = ImageDraw.Draw(samed)

    for box in valid_boxes:
        x1, y1, x2, y2 = box["x1"], box["y1"], box["x2"], box["y2"]
        label = box.get("label", "")

        draw.rectangle([x1, y1, x2, y2], fill="#808080", outline="black", width=3)

        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        font = get_label_font(x2 - x1, y2 - y1)

        if font:
            try:
                draw.text((cx, cy), label, fill="white", anchor="mm", font=font)
            except TypeError:
                bbox = draw.textbbox((0, 0), label, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.text((cx - tw // 2, cy - th // 2), label, fill="white", font=font)
        else:
            draw.text((cx, cy), label, fill="white")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    samed.save(output_path)
    return output_path


# ── SAM overlay (semi-transparent) ───────────────────────────────────


# Per-prompt colours – cycle if more prompts than colours
_SAM_COLORS = [
    (66, 133, 244, 80),   # blue
    (234, 67, 53, 80),    # red
    (251, 188, 4, 80),    # yellow
    (52, 168, 83, 80),    # green
    (171, 71, 188, 80),   # purple
    (255, 112, 67, 80),   # orange
    (0, 172, 193, 80),    # cyan
]

_SAM_BORDER_COLORS = [
    (66, 133, 244, 200),
    (234, 67, 53, 200),
    (251, 188, 4, 200),
    (52, 168, 83, 200),
    (171, 71, 188, 200),
    (255, 112, 67, 200),
    (0, 172, 193, 200),
]


def draw_sam_overlay(
    image: Image.Image,
    valid_boxes: list[dict],
    output_path: str,
) -> str:
    """Draw semi-transparent coloured boxes per prompt on the original image.

    Each unique SAM3 prompt gets a distinct colour.  Box labels are drawn in
    white with a dark outline for readability.  Returns *output_path*.
    """
    overlay = image.convert("RGBA")
    layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # Map prompts → colour index
    prompts_seen: dict[str, int] = {}
    for box in valid_boxes:
        p = box.get("prompt", "")
        if p not in prompts_seen:
            prompts_seen[p] = len(prompts_seen)

    for box in valid_boxes:
        x1, y1, x2, y2 = box["x1"], box["y1"], box["x2"], box["y2"]
        idx = prompts_seen.get(box.get("prompt", ""), 0) % len(_SAM_COLORS)
        fill = _SAM_COLORS[idx]
        border = _SAM_BORDER_COLORS[idx]

        draw.rectangle([x1, y1, x2, y2], fill=fill, outline=border, width=2)

        label = box.get("label", "")
        if label:
            font = get_label_font(x2 - x1, y2 - y1)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            if font:
                try:
                    # Dark outline for readability
                    for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
                        draw.text((cx + dx, cy + dy), label, fill=(0, 0, 0, 200), anchor="mm", font=font)
                    draw.text((cx, cy), label, fill=(255, 255, 255, 255), anchor="mm", font=font)
                except TypeError:
                    bbox = draw.textbbox((0, 0), label, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    tx, ty = cx - tw // 2, cy - th // 2
                    for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
                        draw.text((tx + dx, ty + dy), label, fill=(0, 0, 0, 200), font=font)
                    draw.text((tx, ty), label, fill=(255, 255, 255, 255), font=font)
            else:
                draw.text((cx, cy), label, fill=(255, 255, 255, 255))

    composited = Image.alpha_composite(overlay, layer)
    overlay.close()
    layer.close()
    result = composited.convert("RGB")
    composited.close()

    # Draw legend in top-left
    legend_draw = ImageDraw.Draw(result)
    try:
        legend_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except (OSError, IOError):
        legend_font = ImageFont.load_default()

    y_offset = 8
    for prompt_name, idx in prompts_seen.items():
        c = _SAM_BORDER_COLORS[idx % len(_SAM_BORDER_COLORS)]
        rgb = (c[0], c[1], c[2])
        legend_draw.rectangle([8, y_offset, 22, y_offset + 14], fill=rgb, outline="black")
        legend_draw.text((28, y_offset), prompt_name, fill="black", font=legend_font)
        y_offset += 20

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path)
    return output_path


# ── OCR overlay ─────────────────────────────────────────────────────


def draw_ocr_overlay(
    image: Image.Image,
    ocr_texts: list[dict],
    output_path: str,
) -> str:
    """Draw OCR-detected text bounding boxes and recognized text on the image.

    Each text region is outlined in green with the recognized text displayed
    above the bounding box.  Returns *output_path*.
    """
    overlay = image.copy().convert("RGB")
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for ocr in ocr_texts:
        text = ocr.get("text", "")
        if not text or "x1" not in ocr:
            continue
        x1, y1, x2, y2 = int(ocr["x1"]), int(ocr["y1"]), int(ocr["x2"]), int(ocr["y2"])

        # Green bounding box
        draw.rectangle([x1, y1, x2, y2], outline=(0, 200, 0), width=2)

        # Text label above the box (with dark background for readability)
        conf = ocr.get("confidence", 0)
        display = f"{text} ({conf:.2f})" if conf else text
        try:
            tb = draw.textbbox((x1, y1 - 16), display, font=font)
            draw.rectangle([tb[0] - 1, tb[1] - 1, tb[2] + 1, tb[3] + 1], fill=(0, 0, 0, 180))
        except (AttributeError, TypeError, ValueError):
            pass
        draw.text((x1, y1 - 16), display, fill=(0, 255, 0), font=font)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    overlay.save(output_path)
    return output_path


# ── Composite image builder ──────────────────────────────────────────


def _flatten_to_rgb(img: Image.Image) -> Image.Image:
    """Convert an image to RGB with a white background.

    Handles RGBA/LA/P images by compositing onto a white canvas so that
    transparent areas become white instead of black.
    """
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        background = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[3])  # use alpha as mask
        return background
    return img.convert("RGB")


def build_composite_image(
    image_a: Image.Image,
    image_b: Image.Image,
    direction: str = "horizontal",
) -> Image.Image:
    """Concatenate two images side-by-side (horizontal) or stacked (vertical).

    Used to combine figure.png + samed.png into a single image for multimodal
    LLM calls when the API only accepts one image input.  Images with
    transparency are flattened onto a white background first.
    """
    if direction == "horizontal":
        # Normalise heights
        h = max(image_a.height, image_b.height)
        a = _flatten_to_rgb(image_a.resize((int(image_a.width * h / image_a.height), h)))
        b = _flatten_to_rgb(image_b.resize((int(image_b.width * h / image_b.height), h)))
        composite = Image.new("RGB", (a.width + b.width, h), "white")
        composite.paste(a, (0, 0))
        composite.paste(b, (a.width, 0))
    else:
        w = max(image_a.width, image_b.width)
        a = _flatten_to_rgb(image_a.resize((w, int(image_a.height * w / image_a.width))))
        b = _flatten_to_rgb(image_b.resize((w, int(image_b.height * w / image_b.width))))
        composite = Image.new("RGB", (w, a.height + b.height), "white")
        composite.paste(a, (0, 0))
        composite.paste(b, (0, a.height))
    return composite


# ── JSON extraction ──────────────────────────────────────────────────


def extract_json_block(text: str) -> str:
    """Extract JSON content from a ```json code fence, or return raw text."""
    match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Determine whether to try array or object first based on which appears first
    arr_start = text.find("[")
    obj_start = text.find("{")
    # Try each in order of appearance; this ensures bare arrays (SAM responses)
    # are handled without breaking object extraction (review responses)
    attempts = []
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        attempts = [("[", "]", arr_start), ("{", "}", obj_start)]
    elif obj_start != -1:
        attempts = [("{", "}", obj_start), ("[", "]", arr_start)]
    for open_ch, close_ch, start in attempts:
        if start == -1:
            continue
        for end in range(len(text), start, -1):
            candidate = text[start:end]
            if candidate.rstrip().endswith(close_ch):
                try:
                    json.loads(candidate)
                    return candidate.strip()
                except json.JSONDecodeError:
                    continue
    return text


# ── Review JSON parsing ──────────────────────────────────────────────


def _parse_review_json(response: str, agent_type: str) -> dict:
    """Parse a review JSON response, with fallback for malformed responses."""
    result = None

    # Try to extract JSON from code fence or raw response
    try:
        json_text = extract_json_block(response)
        result = json.loads(json_text)
        # If we got a list (e.g. [{"dimensions":...}]), unwrap first dict element
        if isinstance(result, list):
            result = next((item for item in result if isinstance(item, dict)), None)
    except (json.JSONDecodeError, Exception):
        pass

    if result is None:
        # Fallback: try to find JSON object in the response
        match = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if result is None:
        # Last resort: construct a minimal feedback dict — use 0.0 so consensus
        # routes to "refine" rather than silently accepting fabricated scores.
        score = 0.0
        score_match = re.search(r'"overall_score"\s*:\s*(\d+(?:\.\d+)?)', response)
        if score_match:
            score = float(score_match.group(1))

        if agent_type == "architect":
            result = {
                "overall_score": score,
                "dimensions": {},
                "code_comments": [],
                "verdict": "NEEDS_IMPROVEMENT",
                "primary_issue": "parse_error",
                "parse_failure": True,
                "improvement_instructions": f"Could not parse structured feedback. Raw: {response[:500]}",
            }
        else:
            result = {
                "overall_score": score,
                "dimensions": {},
                "code_comments": [],
                "verdict": "NEEDS_IMPROVEMENT",
                "severity": "moderate",
                "parse_failure": True,
                "key_critique": f"Could not parse structured feedback. Raw: {response[:500]}",
                "counter_suggestions": [],
            }

    # Ensure code_comments is always present
    if "code_comments" not in result:
        result["code_comments"] = []

    return result


# ── Issue merging ────────────────────────────────────────────────────


def validate_text_boundaries(svg_code: str) -> list[str]:
    """Check for text elements that may overflow the SVG viewBox.

    Parses <text> elements, extracts x/y coordinates, and estimates text width
    based on character count. Returns a list of warning strings for elements
    that appear to overflow.
    """
    warnings = []

    # Get viewBox dimensions
    vb_w, vb_h = get_svg_dimensions(svg_code)
    if not vb_w or not vb_h:
        return warnings

    # Find all <text> elements with x/y attributes
    # Match <text ...>content</text> — simplified, handles most cases
    for m in re.finditer(
        r'<text\b([^>]*)>(.*?)</text>',
        svg_code,
        re.DOTALL | re.IGNORECASE,
    ):
        attrs = m.group(1)
        content = re.sub(r'<[^>]+>', '', m.group(2)).strip()  # strip inner tags like <tspan>
        if not content:
            continue

        # Extract x coordinate
        x_match = re.search(r'\bx=["\']([^"\']+)["\']', attrs)
        if not x_match:
            continue
        try:
            x = float(x_match.group(1).split()[0])  # take first value if multiple
        except (ValueError, IndexError):
            continue

        # Extract font-size (default ~16px)
        fs_match = re.search(r'font-size=["\']?(\d+(?:\.\d+)?)', attrs)
        font_size = float(fs_match.group(1)) if fs_match else 16.0

        # Estimate text width: ~0.6 × font_size per character
        estimated_width = len(content) * font_size * 0.6
        anchor_match = re.search(r'\btext-anchor=["\']([^"\']+)["\']', attrs)
        text_anchor = (anchor_match.group(1).strip().lower() if anchor_match else "start")

        # Extract id for reference
        id_match = re.search(r'\bid=["\']([^"\']+)["\']', attrs)
        elem_id = id_match.group(1) if id_match else "unnamed"

        if text_anchor == "middle":
            left = x - estimated_width / 2
            right = x + estimated_width / 2
        elif text_anchor == "end":
            left = x - estimated_width
            right = x
        else:
            left = x
            right = x + estimated_width

        if right > vb_w:
            overflow = right - vb_w
            warnings.append(
                f"Text '{content[:30]}' (id={elem_id}) at x={x:.0f} "
                f"estimated width={estimated_width:.0f}px overflows viewBox "
                f"width={vb_w:.0f} by ~{overflow:.0f}px"
            )
        if left < 0:
            warnings.append(
                f"Text '{content[:30]}' (id={elem_id}) at x={x:.0f} "
                f"starts before viewBox left edge"
            )

    return warnings


# ---------------------------------------------------------------------------
# Structured post-render checks
# ---------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _safe_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bbox_union(boxes: list[dict]) -> dict | None:
    if not boxes:
        return None
    return {
        "x1": min(b["x1"] for b in boxes),
        "y1": min(b["y1"] for b in boxes),
        "x2": max(b["x2"] for b in boxes),
        "y2": max(b["y2"] for b in boxes),
    }


def _parse_points_bbox(points: str) -> dict | None:
    coords = re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', points or "")
    if len(coords) < 4:
        return None
    xs = []
    ys = []
    for i in range(0, len(coords) - 1, 2):
        xs.append(float(coords[i]))
        ys.append(float(coords[i + 1]))
    if not xs or not ys:
        return None
    return {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}


def _parse_path_bbox(path_d: str) -> dict | None:
    tokens = re.findall(r'[MmLlHhVvCcSsQqTtAaZz]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', path_d or "")
    if not tokens:
        return None

    coords: list[tuple[float, float]] = []
    idx = 0
    cmd = None
    cur_x = cur_y = 0.0
    start_x = start_y = 0.0

    def _take_number() -> float | None:
        nonlocal idx
        if idx >= len(tokens):
            return None
        try:
            value = float(tokens[idx])
        except ValueError:
            return None
        idx += 1
        return value

    while idx < len(tokens):
        token = tokens[idx]
        if re.fullmatch(r'[MmLlHhVvCcSsQqTtAaZz]', token):
            cmd = token
            idx += 1
            if cmd in "Zz":
                cur_x, cur_y = start_x, start_y
                coords.append((cur_x, cur_y))
            continue
        if cmd is None:
            idx += 1
            continue

        if cmd in "MmLlTt":
            x = _take_number()
            y = _take_number()
            if x is None or y is None:
                break
            if cmd.islower():
                cur_x += x
                cur_y += y
            else:
                cur_x, cur_y = x, y
            coords.append((cur_x, cur_y))
            if cmd in "Mm":
                start_x, start_y = cur_x, cur_y
                cmd = "l" if cmd == "m" else "L"
        elif cmd in "Hh":
            x = _take_number()
            if x is None:
                break
            cur_x = cur_x + x if cmd == "h" else x
            coords.append((cur_x, cur_y))
        elif cmd in "Vv":
            y = _take_number()
            if y is None:
                break
            cur_y = cur_y + y if cmd == "v" else y
            coords.append((cur_x, cur_y))
        elif cmd in "Cc":
            pts = [_take_number() for _ in range(6)]
            if any(v is None for v in pts):
                break
            x1, y1, x2, y2, x, y = pts
            if cmd == "c":
                coords.extend([
                    (cur_x + x1, cur_y + y1),
                    (cur_x + x2, cur_y + y2),
                ])
                cur_x += x
                cur_y += y
            else:
                coords.extend([(x1, y1), (x2, y2)])
                cur_x, cur_y = x, y
            coords.append((cur_x, cur_y))
        elif cmd in "SsQq":
            pts = [_take_number() for _ in range(4)]
            if any(v is None for v in pts):
                break
            x1, y1, x, y = pts
            if cmd.islower():
                coords.append((cur_x + x1, cur_y + y1))
                cur_x += x
                cur_y += y
            else:
                coords.append((x1, y1))
                cur_x, cur_y = x, y
            coords.append((cur_x, cur_y))
        elif cmd in "Aa":
            pts = [_take_number() for _ in range(7)]
            if any(v is None for v in pts):
                break
            rx, ry, _, _, _, x, y = pts
            if cmd == "a":
                coords.extend([(cur_x - rx, cur_y - ry), (cur_x + rx, cur_y + ry)])
                cur_x += x
                cur_y += y
            else:
                coords.extend([(x - rx, y - ry), (x + rx, y + ry)])
                cur_x, cur_y = x, y
            coords.append((cur_x, cur_y))
        else:
            idx += 1

    if not coords:
        return None
    xs = [pt[0] for pt in coords]
    ys = [pt[1] for pt in coords]
    return {"x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}


def _primitive_bbox(elem: ET.Element) -> dict | None:
    tag = _strip_ns(elem.tag).lower()
    if tag in {"rect", "image"}:
        x = _safe_float(elem.attrib.get("x"))
        y = _safe_float(elem.attrib.get("y"))
        w = _safe_float(elem.attrib.get("width"))
        h = _safe_float(elem.attrib.get("height"))
        if w <= 0 or h <= 0:
            return None
        return {"x1": x, "y1": y, "x2": x + w, "y2": y + h}
    if tag == "circle":
        cx = _safe_float(elem.attrib.get("cx"))
        cy = _safe_float(elem.attrib.get("cy"))
        r = _safe_float(elem.attrib.get("r"))
        if r <= 0:
            return None
        return {"x1": cx - r, "y1": cy - r, "x2": cx + r, "y2": cy + r}
    if tag == "ellipse":
        cx = _safe_float(elem.attrib.get("cx"))
        cy = _safe_float(elem.attrib.get("cy"))
        rx = _safe_float(elem.attrib.get("rx"))
        ry = _safe_float(elem.attrib.get("ry"))
        if rx <= 0 or ry <= 0:
            return None
        return {"x1": cx - rx, "y1": cy - ry, "x2": cx + rx, "y2": cy + ry}
    if tag == "line":
        x1 = _safe_float(elem.attrib.get("x1"))
        y1 = _safe_float(elem.attrib.get("y1"))
        x2 = _safe_float(elem.attrib.get("x2"))
        y2 = _safe_float(elem.attrib.get("y2"))
        return {"x1": min(x1, x2), "y1": min(y1, y2), "x2": max(x1, x2), "y2": max(y1, y2)}
    if tag in {"polygon", "polyline"}:
        return _parse_points_bbox(elem.attrib.get("points", ""))
    if tag == "path":
        return _parse_path_bbox(elem.attrib.get("d", ""))
    return None


def _parse_semantic_group_boxes(svg_code: str) -> list[dict]:
    min_area = load_pipeline_config().get("svg", {}).get("semantic_overlap_min_area", 4000)
    ignore_patterns = (
        r"^scan_vector_",
        r"^embedding_cube_",
        r"^random_masking$",
    )
    try:
        root = ET.fromstring(svg_code)
    except (ET.ParseError, ValueError):
        return []

    groups = []
    for elem in root.iter():
        if _strip_ns(elem.tag).lower() != "g":
            continue
        group_id = elem.attrib.get("id", "")
        if not group_id or any(re.search(pat, group_id) for pat in ignore_patterns):
            continue
        child_boxes = []
        for child in elem.iter():
            if child is elem:
                continue
            bbox = _primitive_bbox(child)
            if bbox is not None:
                child_boxes.append(bbox)
        bbox = _bbox_union(child_boxes)
        if bbox is None or _bbox_area(bbox) < min_area:
            continue
        groups.append({"id": group_id, "bbox": bbox})
    return groups

def _parse_svg_text_elements(svg_code: str) -> list[dict]:
    """Extract all <text> elements with position, font-size, rotation, and estimated bbox."""
    elements = []
    # Match <text ...>content</text> (single-line and multiline)
    text_pattern = re.compile(
        r'<text\b([^>]*)>(.*?)</text>', re.DOTALL
    )
    for m in text_pattern.finditer(svg_code):
        attrs_str, content = m.group(1), m.group(2)
        # Strip inner tags like <tspan>
        content_clean = re.sub(r'<[^>]+>', '', content).strip()
        if not content_clean:
            continue

        def _attr(name: str, default: float = 0.0) -> float:
            match = re.search(rf'{name}=["\']([^"\']+)["\']', attrs_str)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    pass
            return default

        x = _attr("x")
        y = _attr("y")

        # font-size from attribute or style
        font_size = _attr("font-size", 0.0)
        if font_size == 0.0:
            fs_match = re.search(r'font-size:\s*([\d.]+)', attrs_str)
            if fs_match:
                font_size = float(fs_match.group(1))
        if font_size == 0.0:
            font_size = 16.0  # default

        # rotation from transform
        rotation = 0.0
        rot_match = re.search(r'rotate\(\s*([-\d.]+)', attrs_str)
        if rot_match:
            rotation = float(rot_match.group(1))

        anchor_match = re.search(r'text-anchor=["\']([^"\']+)["\']', attrs_str)
        text_anchor = (anchor_match.group(1).strip().lower() if anchor_match else "start")

        # id
        id_match = re.search(r'id=["\']([^"\']+)["\']', attrs_str)
        elem_id = id_match.group(1) if id_match else None

        # Estimate bbox (rough: width ≈ len * font_size * 0.6)
        char_width = font_size * 0.6
        est_width = len(content_clean) * char_width
        est_height = font_size * 1.2

        if abs(rotation) > 45:
            # Rotated text: swap width/height for bbox
            if text_anchor == "middle":
                base_x1 = x - est_height / 2
                base_x2 = x + est_height / 2
                base_y1 = y - est_width / 2
                base_y2 = y + est_width / 2
            elif text_anchor == "end":
                base_x1 = x - est_height / 2
                base_x2 = x + est_height / 2
                base_y1 = y - est_width
                base_y2 = y
            else:
                base_x1 = x - est_height / 2
                base_x2 = x + est_height / 2
                base_y1 = y - est_width
                base_y2 = y
            bbox = {
                "x1": base_x1,
                "y1": base_y1,
                "x2": base_x2,
                "y2": base_y2,
            }
        else:
            if text_anchor == "middle":
                x1 = x - est_width / 2
                x2 = x + est_width / 2
            elif text_anchor == "end":
                x1 = x - est_width
                x2 = x
            else:
                x1 = x
                x2 = x + est_width
            bbox = {
                "x1": x1,
                "y1": y - font_size,
                "x2": x2,
                "y2": y + est_height - font_size,
            }

        elements.append({
            "id": elem_id,
            "content": content_clean,
            "x": x, "y": y,
            "font_size": font_size,
            "rotation": rotation,
            "text_anchor": text_anchor,
            "bbox": bbox,
        })
    return elements


def _bbox_overlap(a: dict, b: dict) -> float:
    """Return intersection area of two bboxes {x1, y1, x2, y2}."""
    dx = max(0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
    dy = max(0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
    return dx * dy


def _bbox_area(b: dict) -> float:
    return max(0, b["x2"] - b["x1"]) * max(0, b["y2"] - b["y1"])


def check_text_overlaps(svg_code: str) -> list[dict]:
    """Detect overlapping text element pairs."""
    overlap_threshold = load_pipeline_config().get("svg", {}).get("overlap_ratio_threshold", 0.3)
    elements = _parse_svg_text_elements(svg_code)
    issues = []
    for i in range(len(elements)):
        for j in range(i + 1, len(elements)):
            a, b = elements[i], elements[j]
            overlap = _bbox_overlap(a["bbox"], b["bbox"])
            min_area = min(_bbox_area(a["bbox"]), _bbox_area(b["bbox"]))
            if min_area > 0 and overlap / min_area > overlap_threshold:
                issues.append({
                    "type": "text_text_overlap",
                    "severity": "high",
                    "message": (
                        f"Text '{a['content'][:30]}' overlaps with "
                        f"'{b['content'][:30]}' "
                        f"(overlap ratio: {overlap / min_area:.0%})"
                    ),
                    "elements": [a["content"][:30], b["content"][:30]],
                    "element_ids": [a.get("id"), b.get("id")],
                    "bboxes": [a["bbox"], b["bbox"]],
                })
    return issues


def check_semantic_element_overlaps(svg_code: str) -> list[dict]:
    """Detect overlaps between large semantic groups such as modules and documents."""
    overlap_threshold = load_pipeline_config().get("svg", {}).get("semantic_overlap_ratio_threshold", 0.18)
    groups = _parse_semantic_group_boxes(svg_code)
    issues = []
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            a, b = groups[i], groups[j]
            overlap = _bbox_overlap(a["bbox"], b["bbox"])
            min_area = min(_bbox_area(a["bbox"]), _bbox_area(b["bbox"]))
            if min_area <= 0:
                continue
            ratio = overlap / min_area
            if ratio > overlap_threshold:
                issues.append({
                    "type": "semantic_element_overlap",
                    "severity": "high",
                    "message": (
                        f"Element '{a['id']}' overlaps with '{b['id']}' "
                        f"(overlap ratio: {ratio:.0%})"
                    ),
                    "elements": [a["id"], b["id"]],
                    "bboxes": [a["bbox"], b["bbox"]],
                })
    return issues


def check_text_boundary_issues(svg_code: str) -> list[dict]:
    """Convert viewBox overflow warnings into structured issues."""
    issues = []
    for warning in validate_text_boundaries(svg_code):
        issues.append({
            "type": "text_boundary_overflow",
            "severity": "high",
            "message": warning,
        })
    return issues


def check_text_in_boxes(
    svg_code: str,
    valid_boxes: list[dict],
    ocr_texts: list[dict],
) -> list[dict]:
    """Verify OCR texts are inside their associated AF boxes in the SVG."""
    issues = []
    text_elements = _parse_svg_text_elements(svg_code)
    svg_texts_lower = {e["content"].lower(): e for e in text_elements}

    for ocr in ocr_texts:
        label = ocr.get("text", "").strip()
        if not label:
            continue
        box_id = ocr.get("assigned_box")
        if not box_id:
            continue

        # Find the matching SVG text element
        svg_el = svg_texts_lower.get(label.lower())
        if not svg_el:
            continue

        # Find the AF box
        box = None
        for b in valid_boxes:
            if b.get("id") == box_id:
                box = b
                break
        if not box:
            continue

        bx1 = box.get("x1", box.get("bbox", [0])[0] if "bbox" in box else 0)
        by1 = box.get("y1", box.get("bbox", [0, 0])[1] if "bbox" in box else 0)
        bx2 = box.get("x2", box.get("bbox", [0, 0, 0])[2] if "bbox" in box else 0)
        by2 = box.get("y2", box.get("bbox", [0, 0, 0, 0])[3] if "bbox" in box else 0)

        tx, ty = svg_el["x"], svg_el["y"]
        if tx < bx1 or tx > bx2 or ty < by1 or ty > by2:
            issues.append({
                "type": "text_outside_box",
                "severity": "high",
                "message": (
                    f"Text '{label[:30]}' at ({tx:.0f},{ty:.0f}) is outside "
                    f"its box {box_id} ({bx1:.0f},{by1:.0f})-({bx2:.0f},{by2:.0f})"
                ),
                "text": label[:30],
                "box_id": box_id,
            })
    return issues


def check_missing_labels(svg_code: str, ocr_texts: list[dict]) -> list[dict]:
    """Find OCR labels that are absent from the SVG."""
    issues = []
    text_elements = _parse_svg_text_elements(svg_code)
    svg_contents_lower = {e["content"].lower() for e in text_elements}

    for ocr in ocr_texts:
        label = ocr.get("text", "").strip()
        if not label or len(label) < 2:
            continue
        if label.lower() not in svg_contents_lower:
            issues.append({
                "type": "missing_label",
                "severity": "medium",
                "message": f"OCR label '{label[:40]}' not found in SVG",
                "label": label[:40],
            })
    return issues


def check_arrow_count(svg_code: str, valid_boxes: list[dict]) -> list[dict]:
    """Compare SAM-detected arrow count vs SVG arrow elements."""
    issues = []
    sam_arrows = sum(
        1 for b in valid_boxes
        if b.get("prompt", "").lower() == "arrow"
    )

    # Count SVG arrows: <line>, <path> with marker-end, or elements with id containing "arrow"
    svg_arrow_markers = len(re.findall(r'marker-end\s*=', svg_code))
    svg_arrow_ids = len(re.findall(r'id=["\'][^"\']*arrow[^"\']*["\']', svg_code, re.IGNORECASE))
    svg_arrows = max(svg_arrow_markers, svg_arrow_ids)

    if sam_arrows > 0 and svg_arrows == 0:
        issues.append({
            "type": "missing_arrows",
            "severity": "high",
            "message": f"SAM detected {sam_arrows} arrows but SVG has none",
            "expected": sam_arrows,
            "actual": svg_arrows,
        })
    elif sam_arrows > 0 and abs(svg_arrows - sam_arrows) > sam_arrows * 0.5:
        issues.append({
            "type": "arrow_count_mismatch",
            "severity": "medium",
            "message": (
                f"Arrow count mismatch: SAM detected {sam_arrows}, "
                f"SVG has ~{svg_arrows}"
            ),
            "expected": sam_arrows,
            "actual": svg_arrows,
        })
    return issues


def check_font_size_sanity(svg_code: str) -> list[dict]:
    """Flag text elements with oversized font (likely OCR bbox bug)."""
    font_threshold = load_pipeline_config().get("svg", {}).get("oversized_font_threshold", 60)
    issues = []
    text_elements = _parse_svg_text_elements(svg_code)
    for el in text_elements:
        if el["font_size"] > font_threshold:
            issues.append({
                "type": "oversized_font",
                "severity": "high",
                "message": (
                    f"Text '{el['content'][:30]}' has font-size "
                    f"{el['font_size']:.0f}px (>60px, likely a bug)"
                ),
                "element": el["content"][:30],
                "font_size": el["font_size"],
            })
    return issues


def run_post_render_checks(
    svg_code: str,
    valid_boxes: list[dict] | None = None,
    ocr_texts: list[dict] | None = None,
) -> dict:
    """Run all structured checks on rendered SVG.

    Returns dict with:
        issues: list of issue dicts
        summary: human-readable string for LLM prompt injection
        counts: dict of issue type -> count
        has_critical: bool (any high-severity issue)
    """
    all_issues: list[dict] = []

    # Always run these
    all_issues.extend(check_text_overlaps(svg_code))
    all_issues.extend(check_semantic_element_overlaps(svg_code))
    all_issues.extend(check_text_boundary_issues(svg_code))
    all_issues.extend(check_font_size_sanity(svg_code))

    # Run if we have context
    if valid_boxes is not None:
        all_issues.extend(check_arrow_count(svg_code, valid_boxes))
    if ocr_texts is not None:
        all_issues.extend(check_missing_labels(svg_code, ocr_texts))
    if valid_boxes is not None and ocr_texts is not None:
        all_issues.extend(check_text_in_boxes(svg_code, valid_boxes, ocr_texts))

    # Build counts
    counts: dict[str, int] = {}
    for issue in all_issues:
        t = issue["type"]
        counts[t] = counts.get(t, 0) + 1

    has_critical = any(i["severity"] == "high" for i in all_issues)
    blocking_types = {"text_text_overlap", "semantic_element_overlap", "text_boundary_overflow"}
    blocking_issues = [i for i in all_issues if i.get("type") in blocking_types]
    has_blocking_overlap = bool(blocking_issues)

    # Build summary string for LLM prompt
    if not all_issues:
        summary = "Post-render checks: ALL PASSED (no issues detected)."
    else:
        lines = [f"Post-render checks: {len(all_issues)} issue(s) found:"]
        for issue in all_issues:
            sev = issue["severity"].upper()
            lines.append(f"  [{sev}] {issue['message']}")
        summary = "\n".join(lines)

    return {
        "issues": all_issues,
        "summary": summary,
        "counts": counts,
        "has_critical": has_critical,
        "blocking_issues": blocking_issues,
        "has_blocking_overlap": has_blocking_overlap,
    }


def build_automated_refinement_instructions(check_result: dict, max_items: int = 8) -> list[str]:
    """Turn deterministic post-render issues into concrete SVG edit instructions."""
    instructions: list[str] = []
    for issue in (check_result or {}).get("issues", [])[:max_items]:
        issue_type = issue.get("type", "")
        if issue_type == "text_text_overlap":
            elems = issue.get("elements", [])
            bboxes = issue.get("bboxes", [])
            if len(elems) == 2 and len(bboxes) == 2:
                instructions.append(
                    f"Separate overlapping labels '{elems[0]}' and '{elems[1]}'. "
                    f"Move, wrap, or reduce the less important annotation so bbox {bboxes[0]} no longer intersects {bboxes[1]}."
                )
            else:
                instructions.append(f"Resolve text-text overlap: {issue.get('message', '')}")
        elif issue_type == "semantic_element_overlap":
            elems = issue.get("elements", [])
            bboxes = issue.get("bboxes", [])
            if len(elems) == 2 and len(bboxes) == 2:
                instructions.append(
                    f"Reduce element-element overlap between '{elems[0]}' and '{elems[1]}'. "
                    f"Reposition, resize, or reroute nearby connectors so bbox {bboxes[0]} no longer intersects {bboxes[1]}."
                )
            else:
                instructions.append(f"Resolve semantic element overlap: {issue.get('message', '')}")
        elif issue_type == "text_boundary_overflow":
            instructions.append(
                f"Fix text clipping against the canvas boundary: {issue.get('message', '')}. "
                "Move the label inward, widen the container, or wrap the text."
            )
        elif issue_type == "oversized_font":
            instructions.append(
                f"Reduce oversized label font and rebalance spacing: {issue.get('message', '')}"
            )
    return instructions


def _merge_issues(arch_feedback: dict, adv_feedback: dict) -> list[dict]:
    """Merge issues from both agents into a sorted list."""
    issues = []

    for dim_name, dim_data in arch_feedback.get("dimensions", {}).items():
        for issue_text in dim_data.get("issues", []):
            issues.append({
                "source": "architect",
                "dimension": dim_name,
                "issue": issue_text,
                "severity": "high" if dim_data.get("score", 2) < 1.0 else "medium",
            })

    for dim_name, dim_data in adv_feedback.get("dimensions", {}).items():
        for issue_text in dim_data.get("issues", []):
            issues.append({
                "source": "advocate",
                "dimension": dim_name,
                "issue": issue_text,
                "severity": "high" if dim_data.get("score", 2) < 1.0 else "medium",
            })

    # Sort: high severity first, then by dimension
    severity_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: (severity_order.get(x["severity"], 2), x["dimension"]))

    return issues
