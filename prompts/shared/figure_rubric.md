# Figure Quality Scoring Rubric (Canonical Source)

See also: `prompts/shared/publication_style.md` for detailed style rules (rcParams, gridlines, backgrounds, palettes).

Score on five dimensions (0-2 each, max 10), then apply a confusion penalty (0 to -2).

| Dimension | 2 (full) | 1 (partial) | 0 (fail) |
|-----------|----------|-------------|----------|
| **Data Accuracy** | Correct data, axes match plan, no missing series, appropriate scale | Minor discrepancy (units, label, scale) | Wrong data, swapped axes, wrong plot type |
| **Clarity** | Main finding clear in 5s, minimal text, no clutter, legend present & unobstructed | Excess labels/annotations, labels overlap, or legend ambiguous | Unreadable, missing labels, or text-heavy figure |
| **Style Consistency** | Approved palette + COLOR_MAP, no gridlines, white background | Palette inconsistent or minor style violation | Default colors, visible gridlines, or colored background |
| **Layout & Composition** | Tight, no clipping, aligned panels, correct aspect ratio | Minor clipping or whitespace | Content clipped, >30% wasted space |
| **Publication Readiness** | Proper fonts (>=7pt), despined (top+right removed), panel labels, colorbar units | One element missing | Multiple issues |

**Confusion penalty** (0 to -2): deduct for points of confusion a naive reader would have.

**Text restraint** (evaluated under Clarity): figures should communicate visually, not verbally. Redundant labels, wordy titles, or unnecessary annotations that could be removed without losing understanding reduce the Clarity score. This is not a separate penalty — it is part of how you assess Clarity above.

**Threshold**: configured in `configs/pipeline.yaml` (`scoring.figure_score_threshold`, default 9.0).
