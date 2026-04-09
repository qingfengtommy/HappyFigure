---
name: figure-planner
description: >
  Given a project directory, produce a detailed figure description document (paper_summary.md)
  with precise per-panel plot-type specifications for every figure a paper needs. This is the
  planning step before figure generation — it scans data files, reads paper text, and outputs
  a structured markdown spec where each panel has an explicit visual form (e.g., "paired-line
  plot", "heatmap matrix", "violin distribution"). Use this skill when the user says "plan my
  figures", "describe what figures I need", "create a figure spec", "write a paper_summary",
  "what plots should I make", or points you at a project directory and wants to know what
  figures to generate before actually generating them. Also use this skill when preparing
  inputs for the figures skill, or when the user has data and a draft but hasn't decided
  on figure layouts yet.
---

# Figure Planner

Scan a project directory and produce a publication-ready figure description document with
precise per-panel plot-type specifications. The output is a `paper_summary.md` that can be
handed directly to a figure-generation workflow — or used by the researcher to plan their
paper's visual narrative.

## Why per-panel precision matters

Through extensive evaluation, we've found that figure generation quality depends almost
entirely on how precisely the input describes each panel's visual form. When the input says
`"1b: quantitative summary"`, the generator defaults to a generic box-and-strip plot. When
it says `"1b: paired-line plot showing titers at Acute and Convalescent with lines connecting
matched subjects"`, the generator produces exactly that. Your job is to produce the precise
version — the one that names the plot type, describes the data structure, and specifies the
visual form unambiguously.

## Workflow

### Step 1: Run the data inspector (one shot)

Start by running the bundled inspection script to get a complete data profile in a single
call. This avoids iterative exploration and gives you everything you need to plan:

```bash
python3 <skill-path>/scripts/inspect_data.py <project-directory>
```

This outputs a JSON summary of every file — column names, row counts, sheet names, data
shapes, and preliminary plot-type suggestions. Read this output once and use it as your
working reference for Steps 3-4.

### Step 2: Inventory the project directory

From the inspector output and a quick directory scan, classify files into categories:

| Category | Extensions / patterns | What to extract |
|----------|----------------------|-----------------|
| **Text** | `.md`, `.txt`, `.tex`, `.docx`, `.pdf` | Paper narrative, figure references, panel descriptions |
| **Tabular data** | `.csv`, `.tsv`, `.xlsx`, `.xls` | Column names, row counts, sheet names, value ranges |
| **Array data** | `.npy`, `.npz`, `.h5`, `.hdf5`, `.pkl`, `.pickle` | Shape, dtype, axis semantics |
| **Structured data** | `.json`, `.yaml`, `.yml`, `.toml` | Config params, experiment metadata |
| **Code** | `.py`, `.R`, `.jl`, `.m` | Architecture definitions, plotting hints, variable names |
| **Images** | `.png`, `.jpg`, `.svg`, `.tif` | Pre-rendered assets that might be composited |
| **Logs** | `events.out.*`, `wandb/`, `*.log` | Training metrics, tensorboard scalars |

Build a file manifest with paths, sizes, and categories. Print it to yourself as a working
reference — don't include it in the output.

### Step 3: Read the text to understand figure needs

Read all text files (drafts, proposals, methods sections, READMEs). Extract:

1. **How many figures** the paper describes (look for "Figure 1", "Fig. 2", etc.)
2. **What each figure is about** (the figure's narrative role in the paper)
3. **What panels are mentioned** (look for "panel a", "(a)", "Fig. 1a", etc.)
4. **Any description of what panels show** (even vague — "a heatmap of gene expression",
   "a comparison of models", "the training curves")
5. **Figure titles or captions** if present

If no text files exist, skip to Step 3 and infer figures purely from data.

### Step 4: Cross-reference data with text

For each data file, run lightweight inspection to understand its structure. The goal is to
determine what plot types the data naturally supports.

**For Excel workbooks** (the most common source for Nature-style papers):
```python
import openpyxl
wb = openpyxl.load_workbook(path, read_only=True)
print(wb.sheetnames)  # Sheet names often map directly to panel labels
for sheet in wb.sheetnames:
    ws = wb[sheet]
    # Read first few rows to get column names and data shape
```

Sheet names like `"Fig. 1a"`, `"1b"`, `"Figure 3"` directly tell you the panel structure.

**For CSVs**: read column names and first few rows. Note the number of rows (matters for
choosing violin vs bar).

**For numpy arrays**: load and check `.shape` and `.dtype`.

**For code files**: scan for plotting functions, figure references, architecture class
definitions.

### Step 5: Determine the plot type for each panel

This is the core of the skill. For each panel, cross-reference the text description (Step 2)
with the data structure (Step 3) to assign a specific plot type.

#### Data-shape heuristics

When the text doesn't specify a visual form, use the data shape to infer one:

| Data pattern | Suggested plot type | How to recognize |
|-------------|-------------------|-----------------|
| 2+ group columns, few rows (<30) | Bar chart with individual data points | Columns named by conditions, ~5-20 values per column |
| 2+ group columns, many rows (>100) | Violin plot or box+strip | Same structure but dense — distributions matter more than means |
| Paired columns (Before/After, Pre/Post, Acute/Conv, WT/Mutant with matched rows) | Paired-line plot | Column pairs where each row is the same subject measured twice |
| Matrix with row and column labels | Heatmap | Rows = features/genes/mAbs, columns = samples/conditions/antigens |
| Two continuous columns (x, y) | Scatter plot | Both columns are numeric, no obvious grouping |
| Column named Trial/Epoch/Day/Time + metric columns | Line plot with error bands | Time-series or learning-curve structure |
| Group column + proportion columns summing to ~1 | Stacked bar or donut/pie | Composition data |
| Columns: ID, log2FC/fold_change, p-value/padj | Volcano plot | Differential expression / enrichment results |
| Columns: time, event/status/censored | Kaplan-Meier survival curve | Time-to-event data |
| Single column or 2 columns with continuous age/gradient | Scatter with continuous colormap | Ordination or gradient-colored embedding |
| 3+ columns: PC1/UMAP1, PC2/UMAP2, label/cluster | Embedding scatter | Dimensionality reduction output |

#### Text-description keywords

When the text DOES describe the panel, look for these keywords to confirm or override
the data-shape heuristic:

| Text keyword | Plot type |
|-------------|-----------|
| "paired", "connected", "matched", "before-after", "within-subject" | Paired-line plot |
| "volcano", "differential", "log-fold", "DE analysis" | Volcano plot |
| "heatmap", "matrix", "binding breadth", "expression profile" | Heatmap |
| "violin", "distribution", "density" | Violin plot |
| "composition", "fraction", "proportion", "breakdown" | Donut/pie or stacked bar |
| "survival", "Kaplan-Meier", "time-to-event" | KM survival curve |
| "ordination", "PCoA", "PCA", "UMAP", "t-SNE" | Embedding scatter |
| "learning curve", "training", "convergence" | Line plot with error bands |
| "network", "chord", "interaction", "co-occurrence" | Network/chord diagram |
| "slope", "ranking shift" | Slope graph |
| "weight loss" + "survival" in same panel | Compound panel (line + KM stacked) |

#### Priority: text > data shape > generic default

If the text says "violin", use violin even if the data has only 20 rows. The paper's
authors chose that form for a reason. If the text is silent, use the data-shape heuristic.
Only fall back to "bar chart with individual data points" when both text and data shape
are ambiguous.

### Step 6: Identify out-of-scope panels

Some panels mentioned in the text won't have matching source data. These are typically:

- **Microscopy / imaging panels** — no tabular source data
- **Structural biology renderings** — 3D protein/molecule images
- **Schematic / workflow diagrams** — conceptual illustrations
- **Panels with missing data** — mentioned in text but no matching file

Mark these explicitly as "out of scope" with a brief reason. This prevents downstream
figure generators from fabricating content.

### Step 7: Write the output document

Produce a `paper_summary.md` file with this structure:

```markdown
# Paper Summary

Title: [paper title]

## Abstract Summary

[1-2 sentence summary of the paper's main finding]

## Figure-Level Context

### Figure N

[1-2 sentence description of this figure's role in the paper]

Reference figure description:

- `Na`: [plot type] showing [what] across [groups/conditions]. [Key visual feature].
- `Nb`: [plot type] of [data] with [details]. [Data source: filename, sheet].
- `Nc`: [out of scope — microscopy/image panel, no source data]
- ...

Publicly drawable:

- `Na`, `Nb`, `Nd`, ...

Out of scope:

- `Nc`, because [reason]

### Figure N+1
...

## Style Guidance

[Domain-appropriate styling notes]
```

#### Per-panel descriptions must include:

1. **The plot type** — the specific visual form (not "quantitative summary")
2. **What the axes/dimensions show** — what's on x, y, color, size
3. **The grouping structure** — what conditions, cohorts, or categories are compared
4. **The defining visual feature** — what makes this panel look like itself
   (e.g., "lines connecting matched subjects", "rows = genes, columns = samples")
5. **Data source** — which file and sheet/column backs this panel
6. **Sample size hint** — if the data has n>100, note it (affects violin vs bar choice)

**Example of a good per-panel description:**
```
- `1b`: paired-line plot showing serum titers at Acute and Convalescent timepoints
  with lines connecting matched subjects within each cohort (4 cohorts × 2 antigens:
  Mich15 H1 and HK14 H3). The within-subject acute-to-convalescent pairing is the
  defining visual feature. [Source: figure1_source_data.xlsx, sheet "1b"]
```

**Example of a bad per-panel description:**
```
- `1b`: quantitative summaries contrasting adults and children
```

The bad version gives a downstream generator no guidance on visual form, so it defaults
to a generic box plot.

### Step 8: Present to the user

Show the complete `paper_summary.md` to the user and ask for corrections before they
use it for figure generation. Common things to verify:

- "Does this panel count look right?"
- "I inferred panel X is a heatmap based on the matrix structure — is that correct?"
- "I couldn't find source data for panels Y and Z — are those image-only?"

---

## Handling edge cases

**No text files in the directory**: Infer figures purely from data file structure. Group
related data files by naming convention (e.g., `figure1_*.csv`, `results_benchmark_*.csv`).
Produce a minimal figure plan based on what the data supports.

**No data files, only text**: Produce the figure description from the text alone, marking
every panel as "data source: to be determined." This is still valuable as a planning document.

**Code-only projects**: Read architecture definitions and plotting scripts. Infer what
figures the code was designed to produce. Note any hardcoded file paths as potential data
sources.

**Very large directories**: Prioritize files matching common patterns (`figure*`, `results*`,
`data/`, `plots/`, `experiments/`). Don't try to read every file in a monorepo.

---

## Domain adaptation

The heuristics above are domain-neutral. But certain domains have signature plot types
that you should recognize from context:

- **Genomics/transcriptomics**: volcano plots, gene-expression heatmaps, pathway enrichment
  bar charts, survival curves
- **Immunology**: paired acute/convalescent, binding-breadth heatmaps, isotype stacked bars
- **Neuroscience**: calcium-imaging traces/violins, behavioral learning curves, brain-region
  bar charts
- **ML/AI**: training loss curves, benchmark grouped bars, attention heatmaps, scaling-law
  line plots, architecture diagrams
- **Physics**: detector response curves, energy spectra, phase diagrams

When you recognize the domain from the text, let it inform your plot-type inference —
but always verify against the actual data shape.
