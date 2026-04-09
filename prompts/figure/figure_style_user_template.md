# Research proposal

{{proposal}}

# Directory tree (this experiment)

{{data_tree}}

# Data schemas (this experiment)

{{data_schemas}}

# Data exploration report (tool-grounded)

{{data_exploration_report}}

# Data structure semantics (this experiment)

{{data_semantics}}

# Directory tree (all experiments, for cross-figure context)

{{full_data_tree}}

# Figure specification for this experiment

{{figure_plan}}

---

Produce a **styled figure specification** that merges the figure plan above with concrete style decisions. The output must be a **single self-contained document** that the code agent can implement directly without needing any other input. Include:

1. Everything from the figure plan (figure type, data mapping, axes, labels, title, legend).
2. Concrete style decisions: exact hex colors per category, font sizes, figure size in inches, subplot layout grid with spacing values.
3. Color-to-category mapping using the palette chosen in the plan's preamble.
4. Panel labels and layout specifics (margins, wspace, hspace).

Do **not** produce a separate "style spec" — integrate all style details directly into the figure specification.

If style reference examples were provided earlier in this conversation, match their visual style closely — use similar colors, fonts, layout proportions, line weights, and overall aesthetic. Extract concrete values (hex colors, font sizes, figure dimensions) from the reference images.

After the prose specification, include a **Style Enforcement Block** in this exact format:

=== STYLE ENFORCEMENT ===
PALETTE: [A|B|C|D]
PALETTE_COLORS: #hex1, #hex2, #hex3, ...
FIGURE_SIZE_TIER: [small|medium|large|composition]
FIGURE_SIZE_INCHES: (W, H)
LAYOUT_GRID: RxC
COLOR_MAP: category1=#hex, category2=#hex, ...
COLOR_MAP_PYTHON:
```python
color_map = {
    "category1": "#hex1",
    "category2": "#hex2",
    ...
}
```
=== END STYLE ENFORCEMENT ===

The `COLOR_MAP_PYTHON` block must be a **complete, copy-paste-ready Python dict** mapping every method/category/group that appears in the data to its exact hex color. The code agent will use this dict directly as `palette=[color_map[m] for m in hue_order]` in seaborn calls. Include ALL variants (e.g. if methods are "F", "F-L", "F-O", "F-T", "H", "H+L", "H+O", "H+T", map each one individually).
