# Publication Style Reference (Canonical Source)

Shared style rules for all figure-generating agents. Referenced by `@code-agent`, `@planner-stylist`, `@figure-critic`, and the orchestrator.

## rcParams Baseline

Every figure script MUST set these matplotlib rcParams:

```python
plt.rcParams.update({
    'font.family': 'sans-serif',
    'font.sans-serif': ['Helvetica', 'Arial', 'DejaVu Sans'],
    'font.size': FONT_BASE_SIZE,          # from styled_spec
    'axes.spines.right': False,
    'axes.spines.top': False,
    'axes.grid': False,                    # NO gridlines
    'axes.facecolor': 'white',            # clean white background
    'axes.linewidth': 0.6,
    'figure.facecolor': 'white',
    'figure.dpi': 300,
    'legend.frameon': False,
    'xtick.major.width': 0.6,
    'ytick.major.width': 0.6,
})
```

## Visual Rules

### Mandatory (violations are blocking)
- **No gridlines**: `axes.grid: False`. Never call `ax.grid(True)`. Clean white background only.
- **Despine**: Remove top and right spines. Only left + bottom visible.
- **White background**: Both `axes.facecolor` and `figure.facecolor` must be white. No gray, no colored backgrounds.
- **Legend frameon=False**: No legend border/background box.
- **DPI 300**: Minimum for publication. Always `savefig(dpi=300, bbox_inches='tight')`.
- **No plt.show()**: Will block in headless execution.

### Strongly preferred (violations reduce quality score)
- **Curated palette only**: Use colors from `color_registry.json` or `palette_reference.md`. Never use matplotlib defaults (tab10, default blue, etc.) for data-encoding colors. Grays (#999999, #CCCCCC) are always allowed for non-data elements (reference lines, annotations).
- **Font hierarchy**: Panel labels > axis labels > tick labels > annotations. Maintain consistent size ratios.
- **Value labels**: Only add when they convey information not readable from the axis. Omit if they cause clutter or overlap. When used, format consistently (same decimal places, same rotation).
- **Minimal text**: Communicate through visual encoding (color, shape, position), not words. Every label must earn its place.
- **Panel labels**: Bold, lowercase (`a`, `b`, `c`), positioned top-left outside the axes area.
- **Tight layout**: Minimal whitespace between panels. `tight_layout()` or `constrained_layout=True`.

### Bar plot specifics
- Flat fill colors, typically no edge color (exception: stacked/hatched bars use `edgecolor='black', linewidth=0.5`)
- Edge darkening (20% darker than fill) is acceptable for visual separation
- Width 0.2–0.35 depending on number of groups
- Y-axis should start at 0 unless ALL values are in a narrow high range AND the axis break is clearly communicated

### Scatter / UMAP specifics
- Marker size 4–15 pt², proportional to data density
- Alpha 0.3–0.8 depending on point count (more points → lower alpha)
- Background points (e.g., "Other") drawn first with lower zorder
- Use curated palette from `palette_reference.md`, not `tab10` or default colormap

### Heatmap / confusion matrix specifics
- Use sequential colormap (`Blues`, `viridis`, `YlGnBu`) — see `palette_reference.md`
- Include colorbar with label
- Re-enable all 4 spines (heatmaps need borders)
- Rotate x-tick labels 90° if text overlaps

### ROC curve specifics
- Diagonal reference line: dashed, light gray (`#CCCCCC`, linewidth=0.5)
- Square aspect ratio (`ax.set_aspect('equal')`)
- AUC value in legend: `f'{model} (AUC = {auc:.3f})'`
- X/Y range: `[-0.02, 1.02]`

## Assembly Rules

- **PIL assembly preferred** (pixel-perfect, no re-rasterization blur)
- Panels auto-cropped (remove white borders before compositing)
- Panels within a row scaled to same height, preserving aspect ratio
- Panel labels: bold, ~14pt (42px at 300 DPI), placed above each panel
- Inter-panel gap: ~30px
- Output: PNG at 300 DPI + PDF

## Color Registry

When `color_registry.json` exists in the run directory, ALL data-encoding colors must come from it. Structure:

```json
{
  "category_group_1": {"name_a": "#hex", "name_b": "#hex"},
  "category_group_2": {"name_c": "#hex", "name_d": "#hex"}
}
```

Code agents read this file and use exact hex values. The critic verifies compliance.
