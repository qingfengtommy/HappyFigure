"""Fallback spec generator for when the LLM planner-stylist agent fails.

Discovers JSON result files, detects comparison structure (methods,
classifiers, categories, flat metrics), and generates styled figure
specifications deterministically in Python.

Used by run_once._step_plan_and_style as a safety net.
"""
from __future__ import annotations

import json
import logging
import math
import os

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PALETTE_A = ["#E64B35", "#4DBBD5", "#00A087", "#3C5488", "#F39B7F", "#8491B4"]

_STYLE_REFS = [
    ("configs/statistical_examples/bar_group_plots/"
     "f4p_figure_CellSpliceNet_comparison.py",
     "multi-panel bar chart with shared legend"),
    ("configs/statistical_examples/bar_group_plots/"
     "f4p_figure_Cflows_fig2_comparison_GeneRegulatory.py",
     "grouped bar comparison across categories"),
    ("configs/statistical_examples/bar_group_plots/"
     "f4p_figure_ImmunoStruct_bars_comparison_Cancer.py",
     "grouped bar with category axis"),
]

_CLASSIFIER_HINTS = {"logistic_regression", "knn", "svm", "random_forest",
                     "linear", "mlp", "xgboost", "lightgbm"}
_METRIC_HINTS = {"accuracy", "f1", "macro_f1", "weighted_f1", "precision",
                 "recall", "auroc", "auc", "mse", "rmse", "mae", "r2"}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_json_results(results_dir: str) -> list[dict]:
    """Discover plottable JSON result files and extract their structure."""
    experiments = []
    search_dirs = [
        os.path.join(results_dir, "results_by_epoch"),
        results_dir,
    ]
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for root, _dirs, files in os.walk(search_dir):
            for fname in sorted(files):
                if not fname.startswith("results_") or not fname.endswith(".json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath) as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(data, dict):
                    continue
                section = data.get("section", "")
                if not section:
                    continue
                if any(e["section"] == section for e in experiments):
                    continue

                exp_info = {
                    "section": section,
                    "path": fpath,
                    "data": data,
                    "methods": [],
                    "classifiers": [],
                    "metrics": {},
                    "categories": [],
                }
                _detect_comparison_structure(exp_info)
                experiments.append(exp_info)
    return experiments


def _detect_comparison_structure(exp: dict) -> None:
    """Analyze JSON data to detect methods, classifiers, metrics, categories."""
    data = exp["data"]

    for key, val in data.items():
        if isinstance(val, dict):
            lower_key = key.lower()
            subkeys = {k.lower() for k in val.keys() if isinstance(val[k], (int, float))}
            if lower_key in _CLASSIFIER_HINTS or subkeys & _METRIC_HINTS:
                if subkeys & _METRIC_HINTS:
                    exp["classifiers"].append(key)

    if "categories" in data and isinstance(data["categories"], dict):
        exp["categories"] = list(data["categories"].keys())

    for group_key in ["clustering", "retrieval", "pairwise"]:
        group = data.get(group_key, {})
        if isinstance(group, dict):
            for k, v in group.items():
                if isinstance(v, (int, float)):
                    exp["metrics"][k] = v

    non_meta_keys = [k for k, v in data.items()
                     if isinstance(v, dict) and k not in ("categories",)
                     and k not in _CLASSIFIER_HINTS
                     and not k.startswith("_")]
    if len(non_meta_keys) >= 2:
        subkey_sets = []
        for k in non_meta_keys:
            if isinstance(data[k], dict):
                subkey_sets.append(set(data[k].keys()))
        if subkey_sets and len(subkey_sets) >= 2:
            common = subkey_sets[0]
            for sks in subkey_sets[1:]:
                common = common & sks
            if len(common) >= 2:
                exp["methods"] = non_meta_keys


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _grid_for_n(n: int) -> tuple[int, int]:
    if n <= 3:
        return 1, n
    if n <= 4:
        return 2, 2
    if n <= 6:
        return 2, 3
    if n <= 8:
        return 2, 4
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return rows, cols


def _size_tier(n_panels: int) -> tuple[str, str]:
    if n_panels <= 1:
        return "small", "(3.5, 2.8)"
    if n_panels <= 2:
        return "medium", "(5.0, 3.5)"
    if n_panels <= 8:
        return "large", "(7.2, 4.5)"
    return "composition", "(7.2, 7.2)"


# ---------------------------------------------------------------------------
# Spec generators
# ---------------------------------------------------------------------------

def _ref_lines() -> str:
    return "\n".join(f"{i+1}. {r[0]} — {r[1]}" for i, r in enumerate(_STYLE_REFS))


def generate_spec(exp: dict) -> tuple[str, str]:
    """Generate a styled spec based on detected structure. Returns (section, spec_text)."""
    methods = exp.get("methods", [])
    classifiers = exp.get("classifiers", [])
    categories = exp.get("categories", [])
    flat_metrics = exp.get("metrics", {})
    title = exp["section"].replace("_", " ").title()
    refs = _ref_lines()

    if methods:
        return _spec_method_comparison(exp, methods, title, refs)
    if classifiers and categories:
        return _spec_classifier_category(exp, classifiers, categories, title, refs)
    if classifiers:
        return _spec_classifier_metrics(exp, classifiers, title, refs)
    if flat_metrics:
        return _spec_flat_metrics(exp, flat_metrics, title, refs)
    return exp["section"], f"# FIGURE SPECIFICATION\n**{title}**\n\nNo plottable structure detected.\n"


def _assign_colors(names: list[str]) -> dict[str, str]:
    return {n: _PALETTE_A[i % len(_PALETTE_A)] for i, n in enumerate(names)}


def _color_table(names: list[str], colors: dict, label_col: str = "Name") -> str:
    return "\n".join(f"| {n} | {colors[n]} | {i} |" for i, n in enumerate(names))


def _color_map_python(names: list[str], colors: dict) -> str:
    entries = "\n".join(f'    "{n}": "{colors[n]}",' for n in names)
    return f"color_map = {{\n{entries}\n}}"


def _color_map_flat(names: list[str], colors: dict) -> str:
    return ", ".join(f"{n}={colors[n]}" for n in names)


def _style_enforcement(palette_colors: str, size: str, grid: str,
                        color_map_flat_str: str, color_map_python_str: str,
                        tier: str = "") -> str:
    return f"""\
## === STYLE ENFORCEMENT ===
PALETTE: A
PALETTE_COLORS: {palette_colors}
{f'FIGURE_SIZE_TIER: {tier}' + chr(10) if tier else ''}FIGURE_SIZE_INCHES: {size}
LAYOUT_GRID: {grid}
COLOR_MAP: {color_map_flat_str}
COLOR_MAP_PYTHON:
```python
{color_map_python_str}
```
=== END STYLE ENFORCEMENT ==="""


_COMMON_STYLE = """\
## Typography

### Font family
- Sans-serif with fallbacks (e.g. Arial, DejaVu Sans)

### Font sizes
- Panel labels: 10 pt, bold
- Axis titles: 8 pt
- Axis tick labels: 7 pt
- Legend text: 7 pt

### Text color
- Nearly black: #1A1A1A"""


_COMMON_AXES = """\
### Spines
- **Visible:** Left & Bottom only
- **Spine width:** 0.5 pt

### Grid
- **None**"""


def _spec_method_comparison(exp: dict, methods: list, title: str, ref_lines: str) -> tuple[str, str]:
    data = exp["data"]
    fpath = exp["path"]
    section = exp["section"]
    labels_abc = "abcdefghijklmnop"

    first_method_data = data[methods[0]]
    all_metrics = {}
    def _collect(d, prefix=""):
        for k, v in d.items():
            if isinstance(v, (int, float)):
                all_metrics[f"{prefix}{k}" if prefix else k] = None
            elif isinstance(v, dict):
                _collect(v, f"{prefix}{k}.")
    _collect(first_method_data)
    metric_names = list(all_metrics.keys())

    n_metrics = len(metric_names)
    rows, cols = _grid_for_n(n_metrics)
    tier, size = _size_tier(n_metrics)
    colors = _assign_colors(methods)

    panels = [f"({labels_abc[i]}) {m}" for i, m in enumerate(metric_names)]
    panel_rows_str = []
    for r in range(rows):
        items = panels[r * cols:(r + 1) * cols]
        while len(items) < cols:
            items.append("[empty]")
        panel_rows_str.append(f"Row {r+1}: {' | '.join(items)}")

    spec = f"""\
# FIGURE SPECIFICATION
**{title}**

---

## Figure purpose
Compare {len(methods)} methods ({', '.join(methods)}) across {n_metrics}
evaluation metrics.

---

## Figure type
**Multi-panel bar chart (small multiples)**
- One panel per metric, {len(methods)} bars per panel (one per method)

---

## Data source & mapping

**File:** {fpath}

**Data extraction**
```python
import json
data = json.load(open("{fpath}"))
methods = {json.dumps(methods)}
results = {{m: data[m] for m in methods}}
```

**Methods (bars, fixed order)**
{chr(10).join(f"{i+1}. `{m}` ({colors[m]})" for i, m in enumerate(methods))}

**Metrics (panels)**
{chr(10).join(f"{i+1}. {m}" for i, m in enumerate(metric_names))}

---

## Layout & sizing
- **Size:** {size} inches ({tier})
- **Grid:** {rows}x{cols}, {n_metrics} panels
- **Arrangement:** {chr(10).join(panel_rows_str)}
- `wspace = 0.35`, `hspace = 0.40`

---

## Axes & scales

### Y-axis — Range: [0, 1.0], Label: "Score"
### X-axis — Categories: {', '.join(methods)}, Label: "Method"

{_COMMON_AXES}

---

## Bars
- **Width:** {min(0.85, 0.85 / max(1, len(methods) - 1)):.2f}, no outline, solid color per method

---

## Color palette & mapping

**Palette A — Nature Reviews**

| Method | Hex | Index |
|--------|-----|-------|
{_color_table(methods, colors)}

---

{_COMMON_STYLE}

---

## Panel labels
- (a) through ({labels_abc[n_metrics-1]}), position (-0.15, 1.05), bold

---

## Legend
- Shared legend at top center, horizontal, no frame

---

## Reference Code
{ref_lines}

---

{_style_enforcement(", ".join(_PALETTE_A), size, f"{rows}x{cols}",
                     _color_map_flat(methods, colors),
                     _color_map_python(methods, colors), tier)}
"""
    return section, spec


def _spec_classifier_category(exp: dict, classifiers: list, categories: list,
                                title: str, ref_lines: str) -> tuple[str, str]:
    data = exp["data"]
    fpath = exp["path"]
    section = exp["section"]

    cat_labels = {c: c.replace("_", " ").title() for c in categories}
    first_cat = data.get("categories", {}).get(categories[0], {})
    first_clf = first_cat.get(classifiers[0], {})
    metric_keys = [k for k, v in first_clf.items() if isinstance(v, (int, float))]

    n_panels = len(metric_keys)
    rows, cols = _grid_for_n(n_panels)
    tier, size = _size_tier(n_panels)
    colors = _assign_colors(classifiers)

    spec = f"""\
# FIGURE SPECIFICATION
**{title}**

---

## Figure purpose
Compare {len(classifiers)} classifiers across {len(categories)} categories.

---

## Figure type
**Multi-panel grouped bar chart**
- {n_panels} panels (one per metric), {len(categories)} groups, {len(classifiers)} bars per group

---

## Data source & mapping

**File:** {fpath}

**Data extraction**
```python
import json, pandas as pd
data = json.load(open("{fpath}"))
rows = []
for cat_name, cat_data in data["categories"].items():
    for clf in {json.dumps(classifiers)}:
        for metric in {json.dumps(metric_keys)}:
            rows.append({{"category": cat_name, "classifier": clf, "metric": metric, "value": cat_data[clf][metric]}})
df = pd.DataFrame(rows)
```

**Classifiers (hue):** {chr(10).join(f"{i+1}. `{c}` ({colors[c]})" for i, c in enumerate(classifiers))}
**Categories (x-axis):** {', '.join(cat_labels.values())}
**Metrics (panels):** {', '.join(metric_keys)}

---

## Layout & sizing
- **Size:** {size} inches ({tier})
- **Grid:** {rows}x{cols}, {n_panels} panels
- `wspace = 0.35`, `hspace = 0.40`

---

## Axes & scales

### Y-axis — Range: [0, 1.0], Label: "Score"
### X-axis — Categories: {", ".join(cat_labels.values())}

{_COMMON_AXES}

---

## Bars
- **Width:** 0.35 per bar, no outline, solid color per classifier

---

## Color palette & mapping

| Classifier | Hex | Label |
|------------|-----|-------|
{_color_table(classifiers, colors)}

---

{_COMMON_STYLE}

---

## Legend
- Top center, horizontal, no frame

---

## Reference Code
{ref_lines}

---

{_style_enforcement(", ".join(_PALETTE_A), size, f"{rows}x{cols}",
                     _color_map_flat(classifiers, colors),
                     _color_map_python(classifiers, colors), tier)}
"""
    return section, spec


def _spec_classifier_metrics(exp: dict, classifiers: list,
                               title: str, ref_lines: str) -> tuple[str, str]:
    data = exp["data"]
    fpath = exp["path"]
    section = exp["section"]

    metric_keys = []
    for clf in classifiers:
        for k, v in data.get(clf, {}).items():
            if isinstance(v, (int, float)) and k not in metric_keys:
                metric_keys.append(k)

    colors = _assign_colors(classifiers)

    spec = f"""\
# FIGURE SPECIFICATION
**{title}**

---

## Figure purpose
Compare {len(classifiers)} classifiers on {len(metric_keys)} metrics.

---

## Figure type
**Grouped bar chart (single panel)**
- {len(metric_keys)} metric groups, {len(classifiers)} bars per group

---

## Data source & mapping

**File:** {fpath}

**Data extraction**
```python
import json
data = json.load(open("{fpath}"))
rows = [(clf, metric, data[clf][metric]) for clf in {json.dumps(classifiers)} for metric in {json.dumps(metric_keys)}]
```

**Classifiers (hue):** {', '.join(f"`{c}` ({colors[c]})" for c in classifiers)}
**Metrics (x-axis):** {', '.join(metric_keys)}

---

## Layout & sizing
- **Size:** (5.0, 3.5) inches (medium)
- **Grid:** 1x1

---

## Axes & scales

### Y-axis — Range: [0, 1.0], Label: "Score"
### X-axis — Categories: {", ".join(metric_keys)}

{_COMMON_AXES}

---

## Bars
- **Width:** 0.35, no outline, solid color per classifier
- **Value annotations:** on top of each bar (3 decimal places, 6pt)

---

## Color palette & mapping

| Classifier | Hex | Label |
|------------|-----|-------|
{_color_table(classifiers, colors)}

---

{_COMMON_STYLE}

---

## Legend
- Top center, horizontal, no frame

---

## Reference Code
{ref_lines}

---

{_style_enforcement(", ".join(_PALETTE_A), "(5.0, 3.5)", "1x1",
                     _color_map_flat(classifiers, colors),
                     _color_map_python(classifiers, colors), "medium")}
"""
    return section, spec


def _spec_flat_metrics(exp: dict, flat_metrics: dict,
                        title: str, ref_lines: str) -> tuple[str, str]:
    fpath = exp["path"]
    section = exp["section"]
    data = exp["data"]
    labels_abc = "abcdefghijklmnop"

    metric_names = list(flat_metrics.keys())
    n_metrics = len(metric_names)
    rows, cols = _grid_for_n(n_metrics)
    tier, size = _size_tier(n_metrics)
    colors = _assign_colors(metric_names)

    y_max = max(flat_metrics.values()) if flat_metrics else 1.0
    y_max_r = 1.0 if y_max <= 1.0 else math.ceil(y_max * 10) / 10

    group_keys = [k for k in ["clustering", "retrieval", "pairwise"] if k in data]
    if group_keys:
        extract_code = f'import json\ndata = json.load(open("{fpath}"))\nmetrics = {{}}\nfor group in {json.dumps(group_keys)}:\n    for k, v in data.get(group, {{}}).items():\n        if isinstance(v, (int, float)):\n            metrics[k] = v'
    else:
        extract_code = f'import json\ndata = json.load(open("{fpath}"))\nmetrics = {{k: v for k, v in data.items() if isinstance(v, (int, float))}}'

    spec = f"""\
# FIGURE SPECIFICATION
**{title}**

---

## Figure purpose
Evaluate performance across {n_metrics} metrics.

---

## Figure type
**Single-series bar chart (small multiples)**
- One panel per metric, one bar per panel, color encodes metric identity

---

## Data source & mapping

**File:** {fpath}

**Data extraction**
```python
{extract_code}
```

**Metrics (panels)**
{chr(10).join(f"{i+1}. {m} = {flat_metrics[m]:.4f}" for i, m in enumerate(metric_names))}

---

## Layout & sizing
- **Size:** {size} inches ({tier})
- **Grid:** {rows}x{cols}, {n_metrics} panels
- `wspace = 0.35`, `hspace = 0.40`

---

## Axes & scales

### Y-axis — Range: [0, {y_max_r}], Label: "Score"
### X-axis — single bar per panel (metric name as title)

{_COMMON_AXES}

---

## Bars
- **Width:** 0.5, no outline, solid color per metric
- **Value annotations:** on top of each bar (3 decimal places, 6pt)

---

## Color palette & mapping

| Metric | Hex | Index |
|--------|-----|-------|
{_color_table(metric_names, colors)}

---

{_COMMON_STYLE}

---

## Panel labels
- (a) through ({labels_abc[n_metrics-1]}), position (-0.15, 1.05), bold

---

## Legend
- No legend needed (one bar per panel)

---

## Reference Code
{ref_lines}

---

{_style_enforcement(", ".join(_PALETTE_A), size, f"{rows}x{cols}",
                     _color_map_flat(metric_names, colors),
                     _color_map_python(metric_names, colors), tier)}
"""
    return section, spec


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_specs_python(run_dir: str, experiments_dir: str) -> list[str]:
    """Generate styled specs deterministically. Returns experiment names."""
    logger.info("Generating styled specs in Python...")
    experiments_root = os.path.join(run_dir, "experiments")
    os.makedirs(experiments_root, exist_ok=True)

    discovered = discover_json_results(experiments_dir)
    if not discovered:
        logger.warning("No JSON result files found in %s", experiments_dir)
        return []

    for exp in discovered:
        structure = []
        if exp["methods"]:
            structure.append(f"methods={exp['methods']}")
        if exp["classifiers"]:
            structure.append(f"classifiers={exp['classifiers']}")
        if exp["categories"]:
            structure.append(f"categories={len(exp['categories'])}")
        if exp["metrics"]:
            structure.append(f"flat_metrics={len(exp['metrics'])}")
        logger.info("%s: %s", exp['section'], ', '.join(structure) or 'unknown')

    experiments = []
    for exp in discovered:
        exp_name, spec_content = generate_spec(exp)
        if "No plottable structure" in spec_content:
            logger.info("Skipping %s: no plottable structure", exp_name)
            continue

        exp_dir = os.path.join(experiments_root, exp_name)
        os.makedirs(exp_dir, exist_ok=True)

        spec_path = os.path.join(exp_dir, "styled_spec.md")
        with open(spec_path, "w") as f:
            f.write(spec_content)

        line_count = spec_content.count("\n")
        logger.info("Wrote experiments/%s/styled_spec.md (%d lines)", exp_name, line_count)
        experiments.append(exp_name)

    # Write multi-figure plan
    plan = generate_multi_figure_plan(discovered, experiments)
    plan_path = os.path.join(run_dir, "multi_figure_plan.md")
    with open(plan_path, "w") as f:
        f.write(plan)
    logger.info("Wrote multi_figure_plan.md")

    return experiments


def generate_multi_figure_plan(discovered: list[dict], experiments: list[str]) -> str:
    lines = [
        "# Multi-Figure Plan",
        "",
        "## Global Style",
        "- **Palette:** A (Nature Reviews)",
        f"- **Colors:** {', '.join(_PALETTE_A)}",
        "- **Font:** Sans-serif with fallbacks",
        "- **Spines:** Left + Bottom only",
        "",
        "## Style References",
    ]
    for i, (ref, note) in enumerate(_STYLE_REFS):
        lines.append(f"{i+1}. `{ref}` — {note}")
    lines.append("")
    lines.append("## Figures")
    lines.append("")
    for exp_info in discovered:
        section = exp_info["section"]
        if section not in experiments:
            continue
        lines.append(f"### {section}")
        lines.append(f"- **Source:** `{exp_info['path']}`")
        if exp_info["methods"]:
            lines.append(f"- **Type:** Multi-method bar ({len(exp_info['methods'])} methods)")
        elif exp_info["classifiers"] and exp_info["categories"]:
            lines.append(f"- **Type:** Grouped bar ({len(exp_info['classifiers'])} classifiers x {len(exp_info['categories'])} categories)")
        elif exp_info["classifiers"]:
            lines.append(f"- **Type:** Grouped bar ({len(exp_info['classifiers'])} classifiers)")
        elif exp_info["metrics"]:
            lines.append(f"- **Type:** Single-series bar ({len(exp_info['metrics'])} metrics)")
        lines.append("")
    return "\n".join(lines)


def update_state_json(run_dir: str, experiments: list[str]) -> None:
    state_path = os.path.join(run_dir, "state.json")
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    else:
        state = {}

    state["per_experiment_specs"] = {
        exp: os.path.join("experiments", exp, "styled_spec.md") for exp in experiments
    }
    state["per_experiment_routes"] = {
        exp: {"figure_category": "statistical", "statistical_subcategory": "bar_group_plots"}
        for exp in experiments
    }
    state["style_references"] = [os.path.basename(r[0]) for r in _STYLE_REFS]
    state.setdefault("completed_steps", [])
    if "figure_plan" not in state["completed_steps"]:
        state["completed_steps"].append("figure_plan")

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    logger.info("Updated state.json with experiment specs")
