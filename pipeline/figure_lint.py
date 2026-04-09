"""Deterministic figure validation — mechanical quality gates.

Runs fast, pure-Python checks *before* the LLM critic to catch obvious
violations that stochastic review misses.  Every function returns a
``LintReport`` and never raises; I/O errors become report issues.

Dependencies beyond stdlib (PIL, numpy) are imported lazily and degrade
gracefully when unavailable.

Typical usage::

    report = lint_figure_output("run_dir/outputs/exp/figure.png")
    if report.blocking:
        print(report.summary())
        for issue in report.issues:
            print(f"  - {issue}")
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# LintReport
# ---------------------------------------------------------------------------


@dataclass
class LintReport:
    """Result of a deterministic lint pass."""

    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """Whether any issue is severe enough to block generation."""
        return not self.passed

    def summary(self) -> str:
        if self.passed and not self.warnings:
            return "All checks passed"
        parts: list[str] = []
        if self.issues:
            parts.append(f"{len(self.issues)} issue(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warning(s)")
        return ", ".join(parts)

    def merge(self, other: LintReport) -> LintReport:
        return LintReport(
            passed=self.passed and other.passed,
            issues=self.issues + other.issues,
            warnings=self.warnings + other.warnings,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"""(?:['"])(\#[0-9a-fA-F]{6})(?:['"])""")
_HEX_BARE_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


def _is_gray(hex_color: str) -> bool:
    """Return True if *hex_color* is a pure gray (R == G == B)."""
    h = hex_color.lstrip("#").lower()
    return len(h) == 6 and h[0:2] == h[2:4] == h[4:6]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _color_distance(c1: tuple[int, int, int], c2: tuple[int, int, int]) -> float:
    """Euclidean distance in RGB space."""
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(c1, c2)))


_COLOR_TOLERANCE = 20.0  # RGB-space distance for "close enough"


def _load_color_registry(path: str) -> list[str] | None:
    """Load a color registry JSON and return a flat list of hex strings, or None.

    Recursively extracts ``#RRGGBB`` strings from any JSON structure
    (flat list, flat dict, or arbitrarily nested dicts/lists).
    """
    try:
        with open(path) as f:
            data = json.load(f)

        colors: list[str] = []

        def _extract(obj: object) -> None:
            if isinstance(obj, str) and obj.startswith("#") and len(obj) == 7:
                colors.append(obj.lower())
            elif isinstance(obj, list):
                for item in obj:
                    _extract(item)
            elif isinstance(obj, dict):
                for v in obj.values():
                    _extract(v)

        _extract(data)
        return colors if colors else None
    except (OSError, ValueError, AttributeError):
        return None


def _color_in_registry(hex_color: str, registry_rgb: list[tuple[int, int, int]]) -> bool:
    """Check if *hex_color* is within tolerance of any registry color."""
    rgb = _hex_to_rgb(hex_color)
    return any(_color_distance(rgb, r) <= _COLOR_TOLERANCE for r in registry_rgb)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. lint_figure_output
# ---------------------------------------------------------------------------


def lint_figure_output(
    figure_path: str,
    *,
    target_size_inches: tuple[float, float] | None = None,
    min_file_bytes: int = 10_000,
    min_dpi: float = 299.5,
) -> LintReport:
    """Post-render validation of a generated figure PNG/SVG.

    Checks file existence, loadability, blankness, file size, and DPI.
    Color compliance is handled at the code level by
    :func:`lint_color_compliance` (pixel-level checks are too noisy due
    to gradients, colormaps, and anti-aliasing).

    Parameters
    ----------
    target_size_inches:
        Expected physical size.  When given, DPI is computed from pixels
        rather than from image metadata.
    min_file_bytes:
        Files smaller than this are flagged as likely blank/corrupt.
    min_dpi:
        Minimum acceptable DPI (from metadata or computed).
    """
    issues: list[str] = []
    warnings: list[str] = []

    # --- existence ---
    if not os.path.isfile(figure_path):
        return LintReport(passed=False, issues=[f"Figure file not found: {figure_path}"])

    # --- file size ---
    try:
        size_bytes = os.path.getsize(figure_path)
        if size_bytes < min_file_bytes:
            issues.append(
                f"Figure is only {size_bytes:,} bytes (expected >= {min_file_bytes:,} — likely blank or corrupt)"
            )
    except OSError as exc:
        issues.append(f"Cannot stat figure file: {exc}")

    # --- SVG fast-path: skip image-based checks ---
    if figure_path.lower().endswith(".svg"):
        return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)

    # --- PIL/numpy checks (lazy import) ---
    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        warnings.append("PIL not available — skipping image-level checks")
        return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)

    try:
        img = Image.open(figure_path)
        img.load()
    except Exception as exc:
        issues.append(f"Image not loadable: {exc}")
        return LintReport(passed=False, issues=issues, warnings=warnings)

    try:
        width_px, height_px = img.size

        # --- blankness (pixel variance) ---
        try:
            import numpy as np  # type: ignore[import-untyped]

            arr = np.asarray(img.convert("RGB"), dtype=np.float32)
            variance = float(np.var(arr))
            if variance < 2.0:
                issues.append(f"Image appears blank (pixel variance {variance:.2f}, threshold 2.0)")
        except ImportError:
            warnings.append("numpy not available — skipping blankness check")
        except Exception as exc:
            warnings.append(f"Blankness check failed: {exc}")

        # --- DPI ---
        dpi_from_meta: float | None = None
        try:
            exif_dpi = img.info.get("dpi")
            if exif_dpi and isinstance(exif_dpi, (tuple, list)) and len(exif_dpi) >= 1:
                dpi_from_meta = float(exif_dpi[0])
        except (AttributeError, IndexError, KeyError):
            pass

        if target_size_inches is not None:
            tw, th = target_size_inches
            if tw > 0 and th > 0:
                effective_dpi = min(width_px / tw, height_px / th)
                if effective_dpi < min_dpi:
                    issues.append(
                        f"Effective DPI is {effective_dpi:.0f} at target size {tw}x{th} in — need >= {min_dpi:.0f}"
                    )
                # Text legibility heuristic: at 6pt minimum, need enough
                # pixels per point.
                min_dim_in = min(tw, th)
                if min_dim_in > 0:
                    px_per_pt = (min(width_px, height_px) / min_dim_in) / 72.0
                    if px_per_pt < 0.5:
                        warnings.append(
                            "Image resolution may be too low for legible 6pt text "
                            f"at target size ({px_per_pt:.1f} px/pt)"
                        )
        elif dpi_from_meta is not None and dpi_from_meta < min_dpi:
            issues.append(f"Image DPI from metadata is {dpi_from_meta:.0f} — need >= {min_dpi:.0f}")
    finally:
        img.close()

    return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)


# ---------------------------------------------------------------------------
# 2. lint_figure_code
# ---------------------------------------------------------------------------

_DESTRUCTIVE_CALLS = re.compile(r"\b(?:os\.remove|os\.unlink|shutil\.rmtree|shutil\.move|os\.rmdir)\s*\(")

_SAVEFIG_RE = re.compile(r"\.savefig\s*\(")
_DPI_IN_SAVEFIG_RE = re.compile(r"\.savefig\s*\([^)]*\bdpi\s*=\s*(\d+)")
_BBOX_TIGHT_RE = re.compile(r"""bbox_inches\s*=\s*['"]tight['"]""")
_BACKEND_RE = re.compile(r"""matplotlib\.use\s*\(\s*['"](?:Agg|agg|PDF|pdf|SVG|svg|Cairo|cairo)['"]""")
_RCPARAMS_RE = re.compile(r"\brcParams\b")
_PLT_SHOW_RE = re.compile(r"\bplt\.show\s*\(")
_PLT_CLOSE_RE = re.compile(r"\bplt\.close\s*\(")

# Nature-quality style enforcement patterns
_DESPINE_TOP_RE = re.compile(
    r"""(?:spines\s*\[\s*['"]top['"]\s*\]\.set_visible\s*\(\s*False\s*\))"""
    r"""|(?:rcParams\s*\[?\s*['"]axes\.spines\.top['"]\s*\]?\s*=\s*False)"""
    r"""|(?:sns\.despine)"""
)
_DESPINE_RIGHT_RE = re.compile(
    r"""(?:spines\s*\[\s*['"]right['"]\s*\]\.set_visible\s*\(\s*False\s*\))"""
    r"""|(?:rcParams\s*\[?\s*['"]axes\.spines\.right['"]\s*\]?\s*=\s*False)"""
    r"""|(?:sns\.despine)"""
)
_LEGEND_FRAMEON_RE = re.compile(
    r"""(?:frameon\s*=\s*False)"""
    r"""|(?:rcParams\s*\[?\s*['"]legend\.frameon['"]\s*\]?\s*=\s*False)"""
)


def lint_figure_code(
    code_path: str,
    *,
    color_registry_path: str | None = None,
    enforce_nature_style: bool = True,
) -> LintReport:
    """Pre-execution validation of figure-generation Python code.

    Checks syntax, backend setting, rcParams usage, savefig parameters,
    destructive calls, publication style enforcement, and optional
    color-registry compliance.

    Parameters
    ----------
    color_registry_path:
        Path to a JSON file whose hex values define the allowed palette.
    enforce_nature_style:
        When True, check for Nature/Science publication patterns
        (despine top+right, legend frameon=False).
    """
    issues: list[str] = []
    warnings: list[str] = []

    if not os.path.isfile(code_path):
        return LintReport(passed=False, issues=[f"Code file not found: {code_path}"])

    try:
        source = _read_file(code_path)
    except Exception as exc:
        return LintReport(passed=False, issues=[f"Cannot read code file: {exc}"])

    # --- syntax check ---
    try:
        compile(source, code_path, "exec")
    except SyntaxError as exc:
        issues.append(f"Syntax error at line {exc.lineno}: {exc.msg}")
        return LintReport(passed=False, issues=issues, warnings=warnings)

    # --- backend ---
    if not _BACKEND_RE.search(source):
        warnings.append("Missing matplotlib.use('Agg') — figure generation may hang in headless mode")

    # --- rcParams ---
    if not _RCPARAMS_RE.search(source):
        warnings.append("No rcParams usage detected — figure may use default matplotlib style")

    # --- savefig ---
    if not _SAVEFIG_RE.search(source):
        issues.append("No savefig() call found — figure will not be saved")
    else:
        dpi_match = _DPI_IN_SAVEFIG_RE.search(source)
        if dpi_match:
            dpi_val = int(dpi_match.group(1))
            if dpi_val < 300:
                issues.append(f"savefig dpi={dpi_val} is below 300 — insufficient for publication")
        else:
            warnings.append("savefig() call does not specify dpi=300")

        if not _BBOX_TIGHT_RE.search(source):
            warnings.append("savefig() missing bbox_inches='tight' — figure may have excessive whitespace")

    # --- anti-patterns ---
    if _PLT_SHOW_RE.search(source):
        issues.append("plt.show() detected — will block/hang in headless execution")

    if not _PLT_CLOSE_RE.search(source):
        warnings.append("No plt.close() call — may leak memory across multiple figure generations")

    # --- destructive operations ---
    for match in _DESTRUCTIVE_CALLS.finditer(source):
        lineno = source[: match.start()].count("\n") + 1
        issues.append(f"Destructive file operation at line {lineno}: {match.group().rstrip('(')}")

    # --- Nature/Science publication style enforcement ---
    if enforce_nature_style:
        if not _DESPINE_TOP_RE.search(source):
            warnings.append("Top spine not removed — publication figures should despine top+right")
        if not _DESPINE_RIGHT_RE.search(source):
            warnings.append("Right spine not removed — publication figures should despine top+right")
        if "legend" in source.lower() and not _LEGEND_FRAMEON_RE.search(source):
            warnings.append("Legend may have a frame — publication style requires frameon=False")
        # Check for grid enablement (Nature figures should not have gridlines)
        grid_patterns = [
            r"\.grid\s*\(\s*True\s*\)",  # ax.grid(True)
            r"\.grid\s*\(\s*visible\s*=\s*True",  # ax.grid(visible=True)
            r"\.grid\s*\(\s*b\s*=\s*True",  # ax.grid(b=True)
            r"rcParams.*axes\.grid.*True",  # rcParams['axes.grid'] = True
        ]
        if any(re.search(p, source) for p in grid_patterns):
            warnings.append("Gridlines enabled — publication figures should have clean white backgrounds")

    # --- color registry ---
    if color_registry_path is not None:
        color_report = lint_color_compliance(code_path, color_registry_path)
        issues.extend(color_report.issues)
        warnings.extend(color_report.warnings)

    return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)


# ---------------------------------------------------------------------------
# 3. lint_styled_spec
# ---------------------------------------------------------------------------

_SPEC_KEYWORD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("data", re.compile(r"\bdata\b", re.IGNORECASE)),
    ("color", re.compile(r"\bcolou?r\b", re.IGNORECASE)),
    ("axes", re.compile(r"\bax(?:es|is)?\b", re.IGNORECASE)),
]


def lint_styled_spec(spec_path: str) -> LintReport:
    """Validate that a styled_spec.md has sufficient structure.

    Checks minimum length, presence of key section keywords, hex color
    definitions, and figure size / DPI mentions.
    """
    issues: list[str] = []
    warnings: list[str] = []

    if not os.path.isfile(spec_path):
        return LintReport(passed=False, issues=[f"Spec file not found: {spec_path}"])

    try:
        text = _read_file(spec_path)
    except Exception as exc:
        return LintReport(passed=False, issues=[f"Cannot read spec file: {exc}"])

    lines = text.splitlines()
    text_lower = text.lower()

    # --- minimum length ---
    if len(lines) < 10:
        issues.append(f"Spec is only {len(lines)} lines (expected >= 10 for a usable spec)")

    # --- required keywords (word-boundary regex to avoid false matches) ---
    missing: list[str] = []
    for label, pattern in _SPEC_KEYWORD_PATTERNS:
        if not pattern.search(text):
            missing.append(label)
    if missing:
        warnings.append(f"Spec missing expected keyword(s): {', '.join(missing)}")

    # --- hex color ---
    if not _HEX_BARE_RE.search(text):
        warnings.append("No hex color (#RRGGBB) found — spec should define a palette")

    # --- figure size ---
    size_patterns = [r"figsize", r"\binches\b", r"\d+\s*[x×]\s*\d+"]
    if not any(re.search(p, text, re.IGNORECASE) for p in size_patterns):
        warnings.append("No figure size specification found (figsize, inches, or dimensions)")

    # --- DPI mention ---
    if "dpi" not in text_lower and "resolution" not in text_lower:
        warnings.append("No DPI or resolution specification found in spec")

    return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)


# ---------------------------------------------------------------------------
# 4. lint_color_compliance
# ---------------------------------------------------------------------------


def lint_color_compliance(code_path: str, color_registry_path: str) -> LintReport:
    """Check that all hex colors in figure code match the color registry."""
    issues: list[str] = []
    warnings: list[str] = []

    if not os.path.isfile(code_path):
        return LintReport(passed=False, issues=[f"Code file not found: {code_path}"])

    registry = _load_color_registry(color_registry_path)
    if registry is None:
        warnings.append(f"Could not load color registry: {color_registry_path}")
        return LintReport(passed=True, warnings=warnings)

    registry_rgb = [_hex_to_rgb(c) for c in registry]

    try:
        source = _read_file(code_path)
    except Exception as exc:
        return LintReport(passed=False, issues=[f"Cannot read code file: {exc}"])

    violations: list[str] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        for match in _HEX_RE.finditer(line):
            hex_color = match.group(1).lower()
            if _is_gray(hex_color):
                continue
            if not _color_in_registry(hex_color, registry_rgb):
                violations.append(f"Line {lineno}: {hex_color} is not in color registry")

    if violations:
        issues.append(f"{len(violations)} off-palette color(s) in code")
        for v in violations[:10]:
            issues.append(f"  {v}")
        if len(violations) > 10:
            issues.append(f"  ... and {len(violations) - 10} more")

    return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)


# ---------------------------------------------------------------------------
# 5. lint_cross_panel_consistency
# ---------------------------------------------------------------------------

_FIGSIZE_RE = re.compile(r"figsize\s*=\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)")
_FONT_SIZE_RE = re.compile(r"""rcParams\s*\[?\s*['"]font\.size['"]\s*\]?\s*=\s*([\d.]+)""")
_SAVEFIG_DPI_RE = re.compile(r"savefig\s*\([^)]*\bdpi\s*=\s*(\d+)")
_SPINE_RE = re.compile(r"""spines\s*\[\s*['"](\w+)['"]\s*\]\.set_visible\s*\(\s*(True|False)\s*\)""")


def lint_cross_panel_consistency(panel_code_paths: dict[str, str]) -> LintReport:
    """Cross-panel mechanical checks across multiple experiment code files.

    Compares figsize, font size, DPI, and spine visibility for consistency.
    """
    issues: list[str] = []
    warnings: list[str] = []

    if len(panel_code_paths) < 2:
        return LintReport(passed=True, warnings=["Need >= 2 panels for consistency check"])

    figsizes: dict[str, tuple[float, float]] = {}
    font_sizes: dict[str, float] = {}
    dpis: dict[str, int] = {}
    spines: dict[str, dict[str, bool]] = {}

    for name, path in panel_code_paths.items():
        try:
            source = _read_file(path)
        except Exception:
            warnings.append(f"Cannot read {name}: {path}")
            continue

        m = _FIGSIZE_RE.search(source)
        if m:
            figsizes[name] = (float(m.group(1)), float(m.group(2)))

        m = _FONT_SIZE_RE.search(source)
        if m:
            font_sizes[name] = float(m.group(1))

        m = _SAVEFIG_DPI_RE.search(source)
        if m:
            dpis[name] = int(m.group(1))

        panel_spines: dict[str, bool] = {}
        for sm in _SPINE_RE.finditer(source):
            panel_spines[sm.group(1)] = sm.group(2) == "True"
        if panel_spines:
            spines[name] = panel_spines

    # --- figsize variance ---
    if len(figsizes) >= 2:
        areas = {n: w * h for n, (w, h) in figsizes.items()}
        min_area = min(areas.values())
        max_area = max(areas.values())
        if min_area > 0 and max_area / min_area > 2.0:
            smallest = min(areas, key=areas.get)  # type: ignore[arg-type]
            largest = max(areas, key=areas.get)  # type: ignore[arg-type]
            issues.append(
                f"Figure sizes vary by > 2x: {smallest} = {figsizes[smallest]}, {largest} = {figsizes[largest]}"
            )

    # --- font size ---
    if len(font_sizes) >= 2:
        vals = list(font_sizes.values())
        if max(vals) - min(vals) > 2.0:
            details = ", ".join(f"{n}={v}" for n, v in font_sizes.items())
            issues.append(f"Base font sizes are inconsistent: {details}")

    # --- DPI ---
    if len(dpis) >= 2:
        unique_dpis = set(dpis.values())
        if len(unique_dpis) > 1:
            details = ", ".join(f"{n}={v}" for n, v in dpis.items())
            warnings.append(f"Inconsistent DPI across panels: {details}")

    # --- spines ---
    if len(spines) >= 2:
        spine_sets = {n: frozenset(s.items()) for n, s in spines.items()}
        unique_configs = set(spine_sets.values())
        if len(unique_configs) > 1:
            warnings.append(
                "Spine visibility settings differ across panels — may look inconsistent in multi-panel layout"
            )

    return LintReport(passed=len(issues) == 0, issues=issues, warnings=warnings)


# ---------------------------------------------------------------------------
# 6. detect_iteration_stuck
# ---------------------------------------------------------------------------


def detect_iteration_stuck(
    results: list[dict[str, Any]],
    *,
    score_tolerance: float = 0.5,
) -> bool:
    """Detect if the critic-iteration loop is stuck or oscillating.

    Parameters
    ----------
    results:
        List of critic result dicts, each expected to have ``"score"``
        (float) and optionally ``"issues"`` (list of strings).
    score_tolerance:
        Scores within this range are considered unchanged.

    Returns
    -------
    True if the loop appears stuck and should be broken early.
    """
    if len(results) < 2:
        return False

    def _score(r: dict[str, Any]) -> float | None:
        s = r.get("score") or r.get("total_score")
        if s is not None:
            try:
                return float(s)
            except (TypeError, ValueError):
                pass
        return None

    def _top_issue(r: dict[str, Any]) -> str | None:
        ti = r.get("top_issue")
        if ti:
            return str(ti).strip().lower()
        iss = r.get("issues")
        if isinstance(iss, list) and iss:
            first = iss[0]
            if isinstance(first, dict):
                return str(first.get("description", first.get("issue", ""))).strip().lower()
            return str(first).strip().lower()
        return None

    def _error(r: dict[str, Any]) -> str | None:
        e = r.get("error")
        return str(e).strip().lower() if e else None

    scores = [_score(r) for r in results]

    # Rule 1: same score (within tolerance) for last 2
    if len(scores) >= 2:
        s1, s2 = scores[-2], scores[-1]
        if s1 is not None and s2 is not None and abs(s1 - s2) <= score_tolerance:
            return True

    # Rule 2: oscillation — up-down-up over last 3
    if len(scores) >= 3:
        s1, s2, s3 = scores[-3], scores[-2], scores[-1]
        if s1 is not None and s2 is not None and s3 is not None:
            if (s2 > s1 and s3 < s2) or (s2 < s1 and s3 > s2):
                return True

    # Rule 3: same top issue for last 2
    if len(results) >= 2:
        i1 = _top_issue(results[-2])
        i2 = _top_issue(results[-1])
        if i1 and i2 and i1 == i2:
            return True

    # Rule 4: same error string for last 2 (from GSD-2 detect-stuck)
    if len(results) >= 2:
        e1 = _error(results[-2])
        e2 = _error(results[-1])
        if e1 and e2 and e1 == e2:
            return True

    return False
