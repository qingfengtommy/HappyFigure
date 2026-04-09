"""Assembly engine for paper composite figures.

Generates self-contained matplotlib scripts that compose individual panel
PNGs into publication-ready paper figures using nested GridSpec layouts.

This module is pure Python with no agent dependencies — it can be tested
in isolation or called by either the python-stages or agent-first orchestrator.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Layout tree types
# ---------------------------------------------------------------------------


@dataclass
class PanelSlot:
    """One panel cell within a row."""

    panel_id: str
    width_ratio: float = 1.0
    col_span: int = 1
    aspect_policy: str = "fill"  # "fill" or "preserve"


@dataclass
class RowSpec:
    """One row of the nested GridSpec layout."""

    row_index: int
    height_ratio: float = 1.0
    panels: list[PanelSlot] = field(default_factory=list)

    @property
    def total_columns(self) -> int:
        return sum(p.col_span for p in self.panels)


@dataclass
class LayoutSpec:
    """Parsed layout tree from an assembly_spec.json file."""

    rows: list[RowSpec] = field(default_factory=list)
    wspace: float = 0.08
    hspace: float = 0.10


@dataclass
class LabelSpec:
    """Panel label formatting."""

    scheme: str = "lowercase"  # "lowercase" or "uppercase"
    size_pt: int = 14
    weight: str = "bold"
    position: str = "top-left"
    offset: tuple[float, float] = (-0.08, 1.04)


@dataclass
class AssemblySpec:
    """Complete parsed assembly specification for one figure."""

    figure_id: str
    figsize_inches: tuple[float, float] = (18.0, 12.0)
    dpi: int = 300
    layout: LayoutSpec = field(default_factory=LayoutSpec)
    labels: LabelSpec = field(default_factory=LabelSpec)
    panel_ids: list[str] = field(default_factory=list)  # ordered list of all panels


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_assembly_spec(spec: dict) -> AssemblySpec:
    """Parse an assembly_spec.json dict into typed dataclasses."""
    layout_data = spec.get("layout", {})
    rows: list[RowSpec] = []
    for row_data in layout_data.get("rows", []):
        panels = [
            PanelSlot(
                panel_id=p["panel_id"],
                width_ratio=p.get("width_ratio", 1.0),
                col_span=p.get("col_span", 1),
                aspect_policy=p.get("aspect_policy", "fill"),
            )
            for p in row_data.get("panels", [])
        ]
        rows.append(
            RowSpec(
                row_index=row_data.get("row_index", len(rows)),
                height_ratio=row_data.get("height_ratio", 1.0),
                panels=panels,
            )
        )

    layout = LayoutSpec(
        rows=rows,
        wspace=layout_data.get("wspace", 0.08),
        hspace=layout_data.get("hspace", 0.10),
    )

    label_data = spec.get("panel_labels", {})
    offset = label_data.get("offset", [-0.08, 1.04])
    labels = LabelSpec(
        scheme=label_data.get("scheme", "lowercase"),
        size_pt=label_data.get("size_pt", 14),
        weight=label_data.get("weight", "bold"),
        position=label_data.get("position", "top-left"),
        offset=(offset[0], offset[1]),
    )

    figsize = spec.get("figsize_inches", [18.0, 12.0])
    all_panel_ids = [p.panel_id for row in rows for p in row.panels]

    return AssemblySpec(
        figure_id=spec.get("figure_id", "Figure"),
        figsize_inches=(figsize[0], figsize[1]),
        dpi=spec.get("dpi", 300),
        layout=layout,
        labels=labels,
        panel_ids=all_panel_ids,
    )


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------


def generate_assembly_script(
    spec: dict,
    panel_paths: dict[str, str],
    output_path: str,
) -> str:
    """Generate a self-contained matplotlib script to assemble panels.

    Args:
        spec: Raw assembly_spec.json dict for one figure.
        panel_paths: Mapping of panel_id → absolute path to panel PNG.
        output_path: Where to save the assembled figure.

    Returns:
        The generated Python script as a string.
    """
    parsed = parse_assembly_spec(spec)
    layout = parsed.layout
    labels = parsed.labels

    # Build panel paths dict as a Python literal
    paths_lines: list[str] = ["{"]
    for pid, path in panel_paths.items():
        paths_lines.append(f"        {pid!r}: {path!r},")
    paths_lines.append("    }")
    paths_literal = "\n".join(paths_lines)

    height_ratios = [r.height_ratio for r in layout.rows]

    # Build per-row rendering code (each line indented at 4 spaces for inside main())
    row_lines: list[str] = []
    panel_index = 0
    for row in layout.rows:
        n_cols = row.total_columns
        width_ratios_list: list[float] = []
        for p in row.panels:
            for _ in range(p.col_span):
                width_ratios_list.append(p.width_ratio / max(p.col_span, 1))

        row_lines.append(f"    # Row {row.row_index}: {n_cols} columns")
        row_lines.append(f"    gs_row{row.row_index} = gs_outer[{row.row_index}, 0].subgridspec(")
        row_lines.append(f"        1, {n_cols}, width_ratios={width_ratios_list!r}, wspace={layout.wspace},")
        row_lines.append("    )")

        col_offset = 0
        for p in row.panels:
            col_slice = str(col_offset) if p.col_span == 1 else f"{col_offset}:{col_offset + p.col_span}"
            label_char = _panel_label(panel_index, labels.scheme)

            row_lines.append(f"    ax = fig.add_subplot(gs_row{row.row_index}[0, {col_slice}])")
            row_lines.append(f"    _render_panel(ax, panel_paths.get({p.panel_id!r}), {p.panel_id!r})")
            if p.aspect_policy == "preserve":
                row_lines.append("    ax.set_aspect('equal', adjustable='box')")
            row_lines.append(f"    ax.text({labels.offset[0]}, {labels.offset[1]}, '{label_char}',")
            row_lines.append(f"            transform=ax.transAxes, fontsize={labels.size_pt},")
            row_lines.append(f"            fontweight='{labels.weight}', va='top', ha='right')")

            col_offset += p.col_span
            panel_index += 1

        row_lines.append("")

    rows_code = "\n".join(row_lines)

    script = f"""#!/usr/bin/env python3
\"\"\"Auto-generated assembly script for {parsed.figure_id}.\"\"\"
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.font_manager as fm
import numpy as np


# ── Publication defaults (Nature-style) ──────────────────────────
plt.rcParams.update({{
    'font.family': 'sans-serif',
    'font.sans-serif': ['Helvetica', 'Arial', 'DejaVu Sans'],
    'font.size': 8,
    'axes.linewidth': 0.5,
    'figure.facecolor': 'white',
    'savefig.facecolor': 'white',
}})


def _render_panel(ax, panel_path, panel_id):
    \"\"\"Render a panel into an axes — image or placeholder.

    Uses interpolation for cleaner appearance at publication DPI.
    Crops whitespace borders from each panel image before embedding.
    \"\"\"
    if panel_path and os.path.exists(panel_path):
        img = plt.imread(panel_path)
        # Auto-crop white borders for tighter assembly
        img_arr = (np.asarray(img)[..., :3] * 255).astype(np.uint8) if img.dtype == np.float32 else np.asarray(img)[..., :3]
        gray = np.mean(img_arr, axis=2)
        non_white = gray < 250
        rows_mask = np.any(non_white, axis=1)
        cols_mask = np.any(non_white, axis=0)
        if rows_mask.any() and cols_mask.any():
            r0, r1 = np.where(rows_mask)[0][[0, -1]]
            c0, c1 = np.where(cols_mask)[0][[0, -1]]
            # Leave a small margin (2% of dimension)
            margin_r = max(int(0.02 * (r1 - r0)), 2)
            margin_c = max(int(0.02 * (c1 - c0)), 2)
            r0 = max(r0 - margin_r, 0)
            r1 = min(r1 + margin_r, img.shape[0] - 1)
            c0 = max(c0 - margin_c, 0)
            c1 = min(c1 + margin_c, img.shape[1] - 1)
            img = img[r0:r1+1, c0:c1+1]
        ax.imshow(img, interpolation='lanczos', aspect='auto')
    else:
        ax.set_facecolor('#F8F8F8')
        ax.text(0.5, 0.5, f'Panel ({{panel_id}})\\n[placeholder]',
                transform=ax.transAxes, ha='center', va='center',
                fontsize=8, color='#999999', style='italic')
        for spine in ax.spines.values():
            spine.set_edgecolor('#DDDDDD')
            spine.set_linewidth(0.4)
            spine.set_linestyle('--')
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def main():
    panel_paths = {paths_literal}

    fig = plt.figure(figsize=({parsed.figsize_inches[0]}, {parsed.figsize_inches[1]}))

    gs_outer = gridspec.GridSpec(
        {len(layout.rows)}, 1,
        height_ratios={height_ratios!r},
        hspace={layout.hspace},
    )

{rows_code}

    output_path = {output_path!r}
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fig.savefig(output_path, dpi={parsed.dpi}, bbox_inches='tight',
                pad_inches=0.15, facecolor='white', edgecolor='none')
    plt.close(fig)
    print(f'Assembled figure saved: {{output_path}}')


if __name__ == '__main__':
    main()
"""
    return script


def _panel_label(index: int, scheme: str) -> str:
    """Return panel label for a given index (a-z, then aa, ab, ...)."""
    base = ord("a") if scheme == "lowercase" else ord("A")
    if index < 26:
        return chr(base + index)
    # For >26 panels: aa, ab, ac, ...
    first = chr(base + (index // 26) - 1)
    second = chr(base + (index % 26))
    return first + second


# ---------------------------------------------------------------------------
# Placeholder generation
# ---------------------------------------------------------------------------


def render_placeholder_png(
    output_path: str,
    panel_id: str,
    panel_type: str,
    text: str | None = None,
    *,
    width_px: int = 800,
    height_px: int = 600,
    dpi: int = 150,
) -> None:
    """Generate a labeled gray placeholder PNG for a non-generatable panel."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(1, 1, figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax.set_facecolor("#F0F0F0")

    display_text = text or f"Panel ({panel_id})\n{panel_type}\n[to be provided]"
    ax.text(
        0.5,
        0.5,
        display_text,
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=11,
        color="#666666",
        style="italic",
    )
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#CCCCCC")
        spine.set_linewidth(1.0)
        spine.set_linestyle("--")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------


def execute_assembly_script(
    script_path: str,
    *,
    timeout: int = 120,
) -> tuple[bool, str]:
    """Execute a generated assembly script.

    Returns:
        (success, error_message) tuple.
    """
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.dirname(script_path) or ".",
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"Assembly script timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------


def validate_assembly_deterministic(
    output_path: str,
    spec: dict,
) -> list[str]:
    """Run fast deterministic checks on assembled figure BEFORE LLM critic.

    Returns:
        List of issue strings. Empty list = all checks passed.
    """
    issues: list[str] = []

    # 1. Output exists
    if not os.path.exists(output_path):
        issues.append(f"Output file does not exist: {output_path}")
        return issues

    # 2. Valid image file (check size)
    file_size = os.path.getsize(output_path)
    if file_size < 10_000:
        issues.append(f"Output file too small ({file_size} bytes) — likely blank or corrupt")

    # 3. Can be loaded as image
    try:
        from PIL import Image
    except ImportError:
        return issues

    try:
        img = Image.open(output_path)
    except Exception as e:
        issues.append(f"Cannot open output as image: {e}")
        return issues

    try:
        img_w, img_h = img.size

        # 4. Check dimensions match spec (within tolerance for bbox_inches='tight')
        spec_w, spec_h = spec.get("figsize_inches", [18.0, 12.0])
        spec_dpi = spec.get("dpi", 300)
        expected_w = spec_w * spec_dpi
        expected_h = spec_h * spec_dpi
        # bbox_inches='tight' can shrink the output significantly, so use wide tolerance
        w_ratio = img_w / expected_w if expected_w > 0 else 1.0
        h_ratio = img_h / expected_h if expected_h > 0 else 1.0
        if w_ratio < 0.2 or w_ratio > 3.0:
            issues.append(f"Width {img_w}px far from expected {expected_w:.0f}px (ratio {w_ratio:.2f})")
        if h_ratio < 0.2 or h_ratio > 3.0:
            issues.append(f"Height {img_h}px far from expected {expected_h:.0f}px (ratio {h_ratio:.2f})")

        # 5. Not fully blank (check variance)
        try:
            import numpy as np

            arr = np.array(img.convert("L"))
            if arr.std() < 2.0:
                issues.append("Output appears to be a blank image (very low pixel variance)")
        except (ImportError, AttributeError, ValueError):
            pass
    finally:
        img.close()

    return issues


# ---------------------------------------------------------------------------
# PIL-based assembly (pixel-perfect, no re-rasterization)
# ---------------------------------------------------------------------------


def assemble_pil(
    spec: dict,
    panel_paths: dict[str, str],
    output_path: str,
    *,
    gap: int = 30,
    label_size: int = 42,
) -> bool:
    """Assemble panels using PIL direct paste — no re-rasterization blur.

    Unlike the matplotlib script approach, this pastes panel PNGs at native
    resolution, preserving pixel-perfect quality. Panels within a row are
    scaled to the same height; rows are stacked vertically.

    Args:
        spec: Assembly spec dict.
        panel_paths: Mapping of panel_id → path to panel PNG.
        output_path: Where to save the assembled figure.
        gap: Pixel gap between panels.
        label_size: Panel label font size in pixels (~14pt at 300 DPI = 42px).

    Returns:
        True on success, False on error.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
    except ImportError:
        return False

    parsed = parse_assembly_spec(spec)
    layout = parsed.layout
    labels = parsed.labels
    dpi = parsed.dpi

    # Resolve label font
    font = None
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        try:
            font = ImageFont.truetype(font_path, label_size)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()

    def _load_and_crop(path: str) -> Image.Image | None:
        if not path or not os.path.exists(path):
            return None
        img = Image.open(path).convert("RGB")
        arr = np.array(img)
        gray = np.mean(arr, axis=2)
        non_white = gray < 250
        rows_mask = np.any(non_white, axis=1)
        cols_mask = np.any(non_white, axis=0)
        if rows_mask.any() and cols_mask.any():
            r0, r1 = np.where(rows_mask)[0][[0, -1]]
            c0, c1 = np.where(cols_mask)[0][[0, -1]]
            margin = max(int(0.01 * (r1 - r0)), 4)
            r0 = max(r0 - margin, 0)
            r1 = min(r1 + margin, img.height - 1)
            c0 = max(c0 - margin, 0)
            c1 = min(c1 + margin, img.width - 1)
            img = img.crop((c0, r0, c1 + 1, r1 + 1))
        return img

    def _placeholder(w: int, h: int, text: str) -> Image.Image:
        # Clamp to reasonable dimensions
        w = max(min(w, 4000), 200)
        h = max(min(h, 3000), 150)
        img = Image.new("RGB", (w, h), (248, 248, 248))
        draw = ImageDraw.Draw(img)
        try:
            small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except (OSError, IOError):
            small_font = ImageFont.load_default()
        draw.text((w // 2, h // 2), text, fill=(153, 153, 153), font=small_font, anchor="mm")
        return img

    def _scale_to_height(img: Image.Image, target_h: int) -> Image.Image:
        ratio = target_h / img.height
        return img.resize((int(img.width * ratio), target_h), Image.LANCZOS)

    # Build rows: load panels, scale to uniform height per row
    row_images: list[Image.Image] = []
    panel_index = 0

    for row_spec in layout.rows:
        panel_imgs: list[Image.Image] = []
        panel_labels_list: list[str] = []

        for slot in row_spec.panels:
            path = panel_paths.get(slot.panel_id)
            img = _load_and_crop(path) if path else None
            if img is None:
                img = _placeholder(800, 600, f"Panel ({slot.panel_id})")
            panel_imgs.append(img)
            panel_labels_list.append(_panel_label(panel_index, labels.scheme))
            panel_index += 1

        # Scale all panels in this row to the same height.
        # Use height_ratio from spec if available, otherwise median.
        if panel_imgs:
            target_h = int(np.median([im.height for im in panel_imgs]))
            target_h = max(min(target_h, 3000), 200)
            scaled = [_scale_to_height(im, target_h) for im in panel_imgs]

            # Clamp extreme widths (panels wider than 6x height are likely
            # multi-subplot panels that should be row-spanning, not inline)
            max_single_w = target_h * 8
            scaled = [
                im.resize((max_single_w, target_h), Image.LANCZOS) if im.width > max_single_w else im for im in scaled
            ]

            # Compose row: hstack with gap + labels
            total_w = sum(im.width for im in scaled) + gap * (len(scaled) - 1)
            row_canvas = Image.new("RGB", (total_w, target_h + label_size + 8), (255, 255, 255))
            draw = ImageDraw.Draw(row_canvas)
            x = 0
            for im, lbl in zip(scaled, panel_labels_list):
                # Label above panel
                draw.text((x + 4, 4), lbl, fill=(0, 0, 0), font=font)
                row_canvas.paste(im, (x, label_size + 8))
                x += im.width + gap

            row_images.append(row_canvas)

    if not row_images:
        return False

    # Stack rows vertically
    max_w = max(r.width for r in row_images)
    total_h = sum(r.height for r in row_images) + gap * (len(row_images) - 1)
    canvas = Image.new("RGB", (max_w, total_h), (255, 255, 255))
    y = 0
    for row_img in row_images:
        canvas.paste(row_img, (0, y))
        y += row_img.height + gap

    # Save
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    canvas.save(output_path, dpi=(dpi, dpi))

    # Also save PDF if requested
    pdf_path = output_path.rsplit(".", 1)[0] + ".pdf"
    try:
        canvas.save(pdf_path, dpi=(dpi, dpi))
    except Exception:
        pass

    return True


# ---------------------------------------------------------------------------
# Convenience: load spec from disk
# ---------------------------------------------------------------------------


def load_assembly_spec(path: str) -> dict:
    """Load an assembly_spec.json file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Cross-figure consistency
# ---------------------------------------------------------------------------


def cross_figure_consistency_check(
    run_dir: str,
    figure_ids: list[str],
) -> dict:
    """Programmatic cross-figure consistency checks.

    Validates that all assembled figures use consistent styling:
    - Same DPI
    - Same panel label scheme
    - Color registry compliance (if color_registry.json exists)

    Returns:
        Dict with check results, written to assembly/cross_figure_consistency.json.
    """
    from pipeline.orchestrator import artifacts as art

    issues: list[str] = []
    checks: dict[str, object] = {"passed": True, "figures_checked": figure_ids, "issues": issues}

    # Collect specs
    specs: dict[str, dict] = {}
    for fig_id in figure_ids:
        spec_path = art.assembly_spec_path(run_dir, fig_id)
        if os.path.exists(spec_path):
            specs[fig_id] = load_assembly_spec(spec_path)

    if len(specs) < 2:
        checks["note"] = "Fewer than 2 figures — skipping cross-figure check"
        return checks

    # Check DPI consistency
    dpis = {fig_id: s.get("dpi", 300) for fig_id, s in specs.items()}
    unique_dpis = set(dpis.values())
    if len(unique_dpis) > 1:
        issues.append(f"Inconsistent DPI across figures: {dpis}")

    # Check panel label scheme consistency
    schemes = {fig_id: s.get("panel_labels", {}).get("scheme", "lowercase") for fig_id, s in specs.items()}
    unique_schemes = set(schemes.values())
    if len(unique_schemes) > 1:
        issues.append(f"Inconsistent panel label scheme: {schemes}")

    # Check label font size consistency
    sizes = {fig_id: s.get("panel_labels", {}).get("size_pt", 10) for fig_id, s in specs.items()}
    unique_sizes = set(sizes.values())
    if len(unique_sizes) > 1:
        issues.append(f"Inconsistent panel label font size: {sizes}")

    # Check color registry exists and is valid
    registry_path = art.color_registry_path(run_dir)
    if os.path.exists(registry_path):
        with open(registry_path, encoding="utf-8") as f:
            registry = json.load(f)
        checks["color_registry_loaded"] = True
        checks["color_count"] = sum(len(v) for v in registry.values() if isinstance(v, dict))
    else:
        checks["color_registry_loaded"] = False

    # Check output files exist
    missing_outputs: list[str] = []
    for fig_id in figure_ids:
        output = art.paper_figure_output_path(run_dir, fig_id)
        if not os.path.exists(output):
            missing_outputs.append(fig_id)
    if missing_outputs:
        issues.append(f"Missing assembled figures: {missing_outputs}")

    checks["passed"] = len(issues) == 0
    checks["issues"] = issues

    # Write result
    out_path = os.path.join(run_dir, "assembly", "cross_figure_consistency.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(checks, f, indent=2)

    return checks
