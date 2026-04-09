### Categorical Palettes (for discrete groups)

Pick **one** categorical palette for the entire figure set. All options are publication-quality and colorblind-safe.

**Option A — Warm Emphasis**
*Muted, saturated, distinct hues. High contrast, print-safe.*

| Index | Name | Hex | RGB |
| :--- | :--- | :--- | :--- |
| 1st | Red-Orange | `#E64B35` | `(230, 75, 53)` |
| 2nd | Cyan-Blue | `#4DBBD5` | `(77, 187, 213)` |
| 3rd | Teal-Green | `#00A087` | `(0, 160, 135)` |
| 4th | Dark Blue | `#3C5488` | `(60, 84, 136)` |
| 5th | Salmon | `#F39B7F` | `(243, 155, 127)` |
| 6th | Cool Gray | `#8491B4` | `(132, 145, 180)` |

**Option B — Okabe-Ito**
*Maximum colorblind accessibility. Preferred when >5 categories or audience includes print-only readers.*

| Index | Name | Hex | RGB |
| :--- | :--- | :--- | :--- |
| 1st | Orange | `#E69F00` | `(230, 159, 0)` |
| 2nd | Sky Blue | `#56B4E9` | `(86, 180, 233)` |
| 3rd | Green | `#009E73` | `(0, 158, 115)` |
| 4th | Yellow | `#F0E442` | `(240, 228, 66)` |
| 5th | Blue | `#0072B2` | `(0, 114, 178)` |
| 6th | Vermilion | `#D55E00` | `(213, 94, 0)` |

**Option C — Muted Professional**
*Same hex values as Option A, rendered at 70% opacity. Produces softer tones when layered.*

| Index | Name | Hex | Alpha |
| :--- | :--- | :--- | :--- |
| 1st | Red-Orange | `#E64B35` | `0.70` |
| 2nd | Cyan-Blue | `#4DBBD5` | `0.70` |
| 3rd | Teal-Green | `#00A087` | `0.70` |
| 4th | Dark Blue | `#3C5488` | `0.70` |
| 5th | Salmon | `#F39B7F` | `0.70` |
| 6th | Cool Gray | `#8491B4` | `0.70` |

*Implementation note:* Apply alpha via RGBA tuples (e.g., `(0.90, 0.29, 0.21, 0.70)`) or `alpha=0.70` in plot calls. Do **not** use 8-digit hex codes (e.g., `#E64B35B2`) — Matplotlib does not reliably parse them.

**Option D — Tableau10-safe**
*High-contrast, colorblind-friendly palette derived from Tableau 10. Replaces the former "Earth Tone" palette whose Cornsilk (#FEFAE0) was invisible on white backgrounds.*

| Index | Name | Hex | RGB |
| :--- | :--- | :--- | :--- |
| 1st | Steel Blue | `#4E79A7` | `(78, 121, 167)` |
| 2nd | Orange Peel | `#F28E2B` | `(242, 142, 43)` |
| 3rd | Brick Red | `#E15759` | `(225, 87, 89)` |
| 4th | Teal | `#76B7B2` | `(118, 183, 178)` |
| 5th | Fern | `#59A14F` | `(89, 161, 79)` |
| 6th | Goldenrod | `#EDC948` | `(237, 201, 72)` |
| 7th | Lavender | `#B07AA1` | `(176, 122, 161)` |
| 8th | Rose | `#FF9DA7` | `(255, 157, 167)` |

### Sequential & Diverging Colormaps (for heatmaps and continuous data)

These are **Matplotlib/Seaborn colormap names**, not categorical palettes. Pass them to the `cmap` parameter.

| Scale Type | When to Use | Recommended `cmap` Values |
| :--- | :--- | :--- |
| **Sequential** | Single-direction numeric gradient (counts, intensity, p-values) | `'Blues'`, `'Greens'`, `'YlGnBu'`, `'viridis'`, `'cividis'` |
| **Diverging** | Values with a meaningful midpoint (correlations, fold-change, anomalies) | `'RdBu_r'` (Blue=Low, Red=High), `'vlag'` |

*Avoid Red/Green diverging colormaps — they are not colorblind-safe.*

### Highlight Strategy (for emphasis — use sparingly)

Use **only** when the figure's purpose is to draw attention to one or two specific categories while de-emphasizing all others. Do NOT use this strategy for general inventory/count charts or when all categories are equally important — in those cases, assign each category a distinct color from the chosen palette (A/B/C/D).

*   Set background categories to `#BFBFBF` (neutral gray).
*   Assign the focal category a single accent color from the chosen palette.
*   Build a color list manually — e.g., `colors = ['#BFBFBF', '#BFBFBF', '#E64B35']`.
*   **Never use `#BFBFBF` for all bars** — that removes visual distinction and makes the chart uninformative.

### Cross-Figure Consistency Rules

1. **Same palette across all figures** — once a palette is chosen (A, B, C, or D), every figure in the project must use it.
2. **Same color mapping** — if a category is assigned the 1st palette color in one figure, it must use the same color in all figures.
3. **Encoding consistency** — prefer consistent category encoding across figures. Additional channels (hatch, outlines, marker shapes, linestyles) are allowed when they improve interpretability.
