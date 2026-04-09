# Figure Quality Scoring Rubric

Score the figure on five dimensions (0–2 points each, 10 max), then apply a confusion penalty (0 to -2).

## Dimensions

### 1. Data Accuracy (0-2)
- **2**: Data correctly loaded, mapped, and rendered; axes match the plan; no missing/extra series; axis scaling (linear vs log) is appropriate for the data distribution.
- **1**: Minor discrepancy (e.g. wrong units, one series mislabeled, linear scale used where log scale would be more appropriate or vice versa).
- **0**: Wrong data file, swapped axes, truncated axes that exaggerate differences, or plot type contradicts the plan.

### 2. Clarity (0-2)
- **2**: A reader can understand the main finding within 5 seconds; no visual clutter; legend is present when multiple series exist and does not obscure data; no redundant labeling (e.g. method names are shown ONLY in the legend, NOT also repeated as x-tick labels on bar plots).
- **1**: Understanding requires effort — axis labels overlap, legend is ambiguous or partially obscures data, excessive grid lines, or method names are redundantly shown in both legend and x-tick labels.
- **0**: Unreadable — overlapping text, missing labels, legend missing entirely when multiple series are present, or no discernible message.

### 3. Accessibility & Style Consistency (0-2)
- **2**: Uses an approved palette (Warm Emphasis A, Okabe-Ito B, Muted Professional C, or Tableau10-safe D) consistently; category-to-color mapping is coherent across related figures.
- **1**: Approved palette used but palette or category mapping is inconsistent with prior figures, OR visual encodings create avoidable clutter.
- **0**: Uses unapproved/default colors; palette choices are not visually distinct.

### 4. Layout and Composition  (0-2)
- **2**: Tight layout with no clipping; appropriate whitespace; panels (if any) aligned and evenly sized; aspect ratio suits the target column width (single-column ~89 mm, double-column ~183 mm) without distorting data proportions. For composition figures with a top legend, minimal vertical gap between the legend and the first row of subplots.
- **1**: Minor clipping or excess whitespace; panels slightly misaligned; aspect ratio slightly off for target width. For composition figures, noticeable vertical gap between top legend and first row of subplots.
- **0**: Critical content clipped or overlapping; wasted space > 30% of canvas; aspect ratio grossly unsuitable or distorts data proportions.

### 5. Publication Readiness (0-2)
- **2**: fonts >= 7 pt at final size, layout is clean without clipping or overlap, Nature-style spines/ticks; multi-panel figures have panel labels (a, b, c…); colorbars include unit annotations.
- **1**: One of the above is missing but easily fixable (e.g. missing panel labels or colorbar units).
- **0**: Multiple issues (huge fonts, no savefig call, wrong format, default matplotlib styling, missing panel labels AND colorbar annotations).

### 6. Confusion Check (deduct 0-2)
After scoring dimensions 1–5, review the figure once more as a naive reader. List every point of confusion you experience — anything that made you pause, squint, or re-read. Deduct points accordingly:

- **0 (no deduction)**: The figure communicates its message without any confusion.
- **-1**: One minor confusion (e.g. unclear what a specific symbol means, ambiguous grouping).
- **-2**: Multiple confusions or one major confusion that undermines the figure's core message.

List each confusion explicitly in the ISSUES section and explain what change would resolve it.


## Quality Thresholds

Score threshold is configured in `configs/pipeline.yaml` (`scoring.figure_score_threshold`, default 9.0).
If no venue is specified, default to the **preprint** threshold.

## Required Output Format

Your response MUST contain all of the following sections:

```
SCORE: X.X

STRENGTHS:
- (one or more bullet points)

ISSUES:
- (one or more bullet points, or "None" if score is 10.0)

ISSUE_CODE:
(Quote the specific code snippets that cause each issue, with fix suggestions. Use fenced code blocks.)

VERDICT: ACCEPT | NEEDS_IMPROVEMENT
```

- **SCORE** must be a number between 0.0 and 10.0 (sum of dimensions 1–5, minus confusion penalty).
- **VERDICT** is ACCEPT when score >= threshold, NEEDS_IMPROVEMENT otherwise.
- Each issue must be specific and actionable (state what is wrong AND how to fix it).
- **ISSUE_CODE** must quote the exact code causing each issue so the code agent can locate and fix it.
