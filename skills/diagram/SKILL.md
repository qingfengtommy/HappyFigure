---
name: diagram
description: >-
  Generate publication-ready method, architecture, and system diagrams as SVG
  from a research proposal using the HappyFigure pipeline. Use this skill
  whenever the user wants to create an architecture diagram, method figure,
  system overview, pipeline visualization, flowchart, block diagram, or any
  structural illustration for a paper — including phrases like "draw my model
  architecture", "I need a method figure", "create a system diagram", "make a
  pipeline figure", "generate an SVG of my approach", "illustrate how the
  system works", or "I need a figure showing the components". Two modes: full
  pipeline (high-fidelity, uses image generation + SAM3 segmentation + SVG
  conversion + team review, requires GPU microservices) and sketch
  (lightweight, agent writes SVG directly from text, no services needed).
---

# Diagram — Method/Architecture Diagram Generation

You are helping the user generate a publication-ready method or architecture
diagram as SVG. There are two modes — your first job is to pick the right one.

## Choosing between `diagram` and `sketch`

### Use `sketch` when:
- The user wants something **quick** — a draft, a first pass, or rapid iteration
- No GPU or microservices are available on this machine
- The diagram is relatively **simple** (linear pipeline, block diagram, basic flowchart)
- The user says things like "quick diagram", "rough sketch", "just draw it"

### Use `diagram` when:
- The user wants **high-fidelity, publication-ready** output (camera-ready, journal submission)
- The diagram is **complex** — many components, icons, detailed annotations
- The user wants to **replicate an existing figure** as SVG (`--drawing-image`)
- GPU microservices (SAM3, OCR, BEN2) are available

**When in doubt, start with `sketch`** — it's faster, needs no services, and
the user can always upgrade to `diagram` later. If the user doesn't specify
quality requirements, suggest sketch first.

## What you need from the user

1. **A proposal file** (markdown) — describes the method, system, or pipeline
   to diagram. Required for `sketch`, optional for `diagram` (but strongly
   recommended).
2. **Quality expectations** — are they drafting or submitting? This determines
   the mode and quality profile.
3. **An existing image** (optional, `diagram` only) — if the user wants to
   convert an existing raster figure to SVG, use `--drawing-image`.

If the user doesn't have a proposal, help them write a short markdown
describing the method/system they want to diagram.

## Running `sketch` (lightweight, no services)

```bash
python cli.py sketch --proposal <path> [--results-dir <dir>] [options]
```

Two-agent flow:
```
Step 1: method-explore agent  → reads proposal, writes method_description.md
Step 2: svg-author agent      → creates SVG directly from text, self-reviews iteratively
```

The svg-author studies reference diagrams from `configs/method_examples/` for
visual style, writes SVG, validates it, renders to PNG, and self-reviews
(up to 3 iterations). No microservices needed.

### Common sketch invocations

```bash
# Quick method diagram from a proposal
python cli.py sketch --proposal paper.md

# With a specific agent
python cli.py sketch --proposal paper.md --agent claude
```

## Running `diagram` (full pipeline, requires services)

```bash
python cli.py diagram --proposal <path> [options]
```

Three-agent flow:
```
Step 1: method-explore agent  → reads proposal, writes method_description.md
Step 2: svg-builder agent     → generates raster image, runs SAM3/OCR/BEN2, builds SVG
Step 3: svg-refiner agent     → compares rendered SVG to source image, fixes issues iteratively
```

The orchestrator manages microservices (SAM3:8001, OCR:8002, BEN2:8003)
automatically — starts them before svg-builder and stops them after.

### Quality profiles

The `--quality-profile` flag sets the review score threshold. Higher thresholds
mean more review iterations, which means better quality but longer runtime.

| Profile | Threshold | When to use |
|---------|-----------|-------------|
| `journal` | 10.2/12 | Camera-ready journal submission (default) |
| `conference` | 9.6/12 | Conference paper |
| `poster` | 8.4/12 | Poster or informal presentation |

Other profiles: `presentation` (7.8), `report` (9.0), `grant` (9.6),
`thesis` (9.6), `preprint` (9.0).

### Image replication mode

If the user has an existing figure they want to convert to SVG:

```bash
# Convert an existing image to SVG (skips method-explore + image generation)
python cli.py diagram --drawing-image existing_figure.png

# Or reuse the image from a previous run
python cli.py diagram --resume-run runs/diagram_runs/run_20260315_143022
```

This skips Step 1 entirely and starts SAM3 segmentation directly on the
provided image.

### Common diagram invocations

```bash
# Full pipeline with default (journal) quality
python cli.py diagram --proposal paper.md

# Conference quality (faster, slightly lower threshold)
python cli.py diagram --proposal paper.md --quality-profile conference

# Replicate an existing figure as SVG
python cli.py diagram --drawing-image my_figure.png

# More review iterations for complex diagrams
python cli.py diagram --proposal paper.md --max-team-iterations 5
```

### Other diagram options

| Flag | Default | When to use |
|------|---------|-------------|
| `--architecture-examples-dir` | `configs/method_examples/` | Custom reference diagrams |
| `--max-team-iterations` | 3 | Increase for complex diagrams that need more review passes |
| `--sam-min-score` | 0.0 | Raise to filter low-confidence SAM3 detections |
| `--optimize-iterations` | 2 | SVG optimization passes (0 to skip) |

## Output

Both modes save to `runs/diagram_runs/run_<timestamp>/`. The key files:

- `method_architecture.svg` — the final SVG diagram
- `method_architecture.png` — rendered PNG of the final diagram
- `method_description.md` — the method description (Step 1 output)
- `review_log.json` — review scores and termination reason

For `diagram` mode, you'll also find intermediate artifacts: `figure.png`
(original raster), `samed.png` (SAM3 boxes), `template.svg`, `final.svg`,
per-iteration renders and review JSONs.

## Troubleshooting

- **Services won't start**: Check that GPU is available. SAM3 and BEN2 need a
  CUDA-capable GPU. If no GPU, use `sketch` instead.
- **Low review scores**: Try `--max-team-iterations 5` or `--quality-profile poster`
  to accept lower scores rather than spinning forever.
- **SVG looks wrong**: Check `logs/agent_svg-builder.log` and
  `logs/agent_svg-refiner.log`. The refiner's `element_check_iter{N}.json`
  files show exactly what it found wrong at each iteration.

## When this is NOT the right skill

- If the user wants **data plots** (bar charts, line graphs, scatter plots from
  experiment results), use the `plot` command instead.
- If the user wants to **hand-edit SVG code**, just help them directly — no need
  for the pipeline.
