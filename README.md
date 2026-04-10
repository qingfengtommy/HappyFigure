# HappyFigure

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

AI-powered scientific figure generation pipeline. HappyFigure takes a research proposal and experimental results, then automatically generates publication-ready statistical figures and method/architecture diagrams.

## Features

- **Statistical figures**: Bar charts, line plots, heatmaps, scatter plots, multi-panel compositions
- **Architecture diagrams**: SVG method drawings from text descriptions, with optional image segmentation
- **Multi-provider LLM support**: Azure, Google Gemini, OpenAI, Anthropic, AWS Bedrock
- **Agent platforms**: Run via OpenCode, Claude Code, Codex CLI, Gemini CLI, or Copilot
- **Configurable pipeline**: All parameters in `configs/pipeline.yaml`
- **Iterative refinement**: Critic-driven loop scores figures on a 5-dimension rubric
- **Beam search**: Explore multiple style/code variants and rank by quality score

## Installation

### Prerequisites

- Python 3.10–3.12 (recommended: 3.12). Python 3.13 works for core features but has no PaddleOCR wheel.
- For GPU-accelerated microservices (diagram mode only): NVIDIA GPU with CUDA drivers

### Setup

```bash
conda create -n happyfigure python=3.12 -y
conda activate happyfigure
cd HappyFigure

# Core install
pip install -e .

# Add your LLM provider(s)
pip install -e ".[google]"        # Google Gemini / Vertex AI
pip install -e ".[anthropic]"     # Anthropic Claude
pip install -e ".[azure]"         # Azure CLI / Managed Identity auth
pip install -e ".[all]"           # All LLM providers + SVG + examples

# Microservices (only needed for `diagram` command)
pip install -e ".[services]"

# Development
pip install -e ".[dev]"
```

### Configure API keys

```bash
cp .env.example .env
# Edit .env — set at least one provider:
#   GEMINI_API_KEY=your-key
#   AZURE_OPENAI_ENDPOINT=... + AZURE_OPENAI_API_KEY=...
#   ANTHROPIC_API_KEY=...
```

## Quick Start

### 1. Generate statistical figures

Point HappyFigure at your research proposal and the directory containing your experimental results (CSV files, JSON outputs, etc.). The pipeline will scan your data, decide what figures to create, and generate publication-ready matplotlib plots — one per experiment.

```bash
# Using file paths (positional — proposal file auto-detected by extension)
python cli.py plot paper.md ./results

# Or with explicit flags
python cli.py plot --proposal paper.md --results-dir ./results

# Or with natural language — just describe what you want
python cli.py plot "generate bar charts comparing model accuracy across datasets"
```

You can pass positional arguments (files are auto-detected as proposals by extension, directories as results) or use explicit `--proposal` / `--results-dir` flags. You can also pass a natural-language instruction instead of a proposal file — the pipeline will forward your description to the agent as the task specification.

Behind the scenes, three AI agents work in sequence. First, a **data-explore** agent reads every file in your results directory, identifies experiments and their schemas, and writes a structured exploration report. Next, a **planner-stylist** agent reads the report along with your proposal, plans the figures, and writes a detailed styled specification for each one (plot type, axis labels, color palette, fonts, etc.). Finally, a **code-agent** generates matplotlib code for each figure and runs it. A built-in **figure-critic** scores the output on five dimensions; if the score is below the threshold, the code-agent revises and re-runs, up to three iterations.

Output: `runs/figure_runs/run_YYYYMMDD_HHMMSS/`

### 2. Generate an architecture diagram

If you need a method or architecture diagram for your paper, HappyFigure can generate one from your proposal text alone — no data directory needed.

**Sketch mode** (lightweight, no GPU required): an agent reads your proposal, writes a method description, then directly authors an SVG diagram with a self-review loop.

```bash
python cli.py sketch --proposal paper.md
```

**Diagram mode** (full pipeline, GPU + microservices required): an LLM generates a raster image of the diagram, then SAM3 segments it into bounding boxes, PaddleOCR extracts text labels, and BEN2 removes backgrounds from icons. These are reassembled into a clean, editable SVG. An advocate agent scores the result and iteratively refines it.

```bash
# Start the three microservices (SAM3, OCR, BEN2)
python scripts/pipeline_cli.py services start

# Run the full diagram pipeline
python cli.py diagram --proposal paper.md

# Stop services when done
python scripts/pipeline_cli.py services stop
```

**Composite mode** extends diagram mode by replacing raster visualization panels (bar charts, heatmaps embedded in the diagram) with programmatically generated versions for higher fidelity.

```bash
python cli.py composite --proposal paper.md
```

Output: `runs/diagram_runs/run_YYYYMMDD_HHMMSS/`

### 3. Generate everything for a paper

The `paper` command is the end-to-end pipeline: it discovers what figures your paper needs (both statistical plots and architecture diagrams), generates each one, then assembles them into final multi-panel figures matching the layout your paper requires.

```bash
python cli.py paper --proposal paper.md --results-dir ./results --execution parallel
```

The pipeline runs four stages: **DISCOVER** (classify what figures the paper needs and which panels are plots vs. diagrams), **PLAN** (create detailed specs for each panel), **GENERATE** (produce every panel in parallel), and **ASSEMBLE** (compose panels into final figures with consistent styling, spacing, and labeling). Assembly is visual-aware — the agent inspects generated panels and reference figures before deciding layout.

Output: `runs/paper_runs/run_YYYYMMDD_HHMMSS/`

### 4. Find your output

Each run creates a timestamped directory with all generated figures, source code, critic scores, and intermediate artifacts.

| Command | Output location |
|---------|----------------|
| `plot` | `runs/figure_runs/run_YYYYMMDD_HHMMSS/` |
| `sketch`, `diagram`, `composite` | `runs/diagram_runs/run_YYYYMMDD_HHMMSS/` |
| `paper` | `runs/paper_runs/run_YYYYMMDD_HHMMSS/` |

Inside a run directory you will find: the copied proposal, an exploration report, per-experiment workspaces (styled specs, generated code, critic results), and an `outputs/` directory with the final promoted figures.

## Commands

| Command | Alias | What it does | Services needed |
|---------|-------|-------------|-----------------|
| `plot` | `figure` | Statistical plots from experiment data | No |
| `sketch` | `method-svg` | Architecture diagram — agent writes SVG directly | No |
| `diagram` | `method` | Architecture diagram — full pipeline (image gen → SAM3 → SVG → review) | Yes |
| `composite` | `hybrid` | Diagram + programmatic visualization compositing | Yes |
| `paper` | `paper-composite` | All figures for a paper (plots + diagrams + assembly) | Diagram panels only |
| `review` | — | Interactively review figures from a completed run | No |

## How It Works

HappyFigure has **two independent LLM layers** that you can mix and match:

```
┌─────────────────────────────────────────────────────────┐
│                      cli.py                             │
├──────────────────────┬──────────────────────────────────┤
│  Agent Platform      │  LLM Providers (pipeline nodes)  │
│  ──────────────────  │  ──────────────────────────────── │
│  Drives multi-step   │  Direct API calls for:           │
│  reasoning via CLI:  │   • chat  (text generation)      │
│   • opencode         │   • code  (code generation)      │
│   • claude           │   • drawing (image generation)   │
│   • codex / gemini   │                                  │
│   • copilot          │  Providers: Azure, Google,       │
│                      │  OpenAI, Anthropic, Bedrock      │
└──────────────────────┴──────────────────────────────────┘
```

- **Agent platform** = which CLI tool runs multi-step reasoning (explores data, writes code, critiques). Pick one; all agents share the same model.
- **LLM providers** = which APIs the pipeline calls for text/code/image generation. Configured per-role.
- These are **independent** — e.g., use Claude Code agents + Google Gemini for image generation.

### Agent flow per command

**`plot`** — 3 agents, no services:

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | `data-explore` | Scans data files, writes exploration report |
| 2 | `planner-stylist` | Plans figures, writes styled specs per experiment |
| 3 | `code-agent` + `figure-critic` | Generates matplotlib code, iterates until quality threshold |

**`diagram`** — 3 agents + image API + microservices:

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | `method-explore` | Reads proposal, writes method description |
| 2 | `svg-builder` | Generates raster image → SAM3 segmentation → SVG |
| 3 | `svg-refiner` | Iteratively improves SVG with advocate scoring |

**`sketch`** — 2 agents, no services:

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | `method-explore` | Reads proposal, writes method description |
| 2 | `svg-author` | Writes SVG directly with self-review loop (max 3 iterations) |

> **Tip**: The `drawing` LLM role (image generation) is **only used by `diagram` and `composite`**. The `plot` and `sketch` commands only use agents.

## Configuration

All config lives in `configs/pipeline.yaml`. The two key sections:

### Agent platform

```yaml
agent:
  platform: opencode          # default: opencode | claude | codex | gemini | copilot
  opencode:
    model: gpt-5.4
    provider: azure
  claude:
    model: claude-opus-4-6
  gemini:
    model: gemini-3.1-pro-preview
```

Override per-run: `python cli.py plot --agent claude --proposal paper.md`

### LLM provider routing

```yaml
llm:
  roles:
    chat:    {provider: azure, model: gpt-5.4}
    code:    {provider: azure, model: gpt-5.4}
    drawing: {provider: azure, model: gpt-image-1}
```

Switch providers per-run with presets:

```bash
python cli.py plot --proposal paper.md --llm-preset gemini   # all roles → Google Gemini
python cli.py plot --proposal paper.md --llm-preset mixed    # Azure text + Gemini drawing
```

See [Configuration Guide](docs/CONFIGURATION.md) for recipes, detailed preset reference, and OpenCode auto-discovery.

## CLI Reference

```bash
python cli.py <command> [options]
python cli.py plot -h              # command-specific help
```

### Input modes

HappyFigure accepts input in three ways:

```bash
# 1. Positional arguments — files and directories are auto-detected
python cli.py plot paper.md ./results          # .md → proposal, directory → results
python cli.py plot paper.md ./exp1 ./exp2      # multiple results directories

# 2. Explicit flags
python cli.py plot --proposal paper.md --results-dir ./results

# 3. Natural language — describe what you want instead of providing a file
python cli.py plot "generate bar charts comparing model accuracy"
python cli.py sketch "draw a transformer architecture with attention layers"
```

When you pass a natural-language string (3+ words that don't resolve to an existing file), HappyFigure forwards it as a task instruction to the agent instead of reading a proposal file.

| Flag | Description | Default |
|------|-------------|---------|
| `--proposal <file>` | Path to paper proposal / description, or a natural-language instruction | **(required)** |
| `--results-dir <dir>` | Directory containing experiment data | `./results` |
| `--agent <platform>` | Agent platform: `opencode`, `claude`, `codex`, `gemini`, `copilot` | from config |
| `--llm-preset <name>` | LLM preset: `azure`, `gemini`, `mixed` | from config |
| `--execution <mode>` | `sequential`, `parallel` (4 threads), or `beam` (variant search) | `sequential` |
| `--resume <run_dir>` | Resume from a previous run directory | — |
| `--review` | Enable human review feedback loop | off |
| `--verbose` | Save prompts and detailed logs to run_dir | off |

### Execution modes

| Mode | What it does | Best for |
|------|-------------|----------|
| `sequential` | One experiment at a time | Debugging, small runs |
| `parallel` | All experiments concurrently (max 4 threads) | Normal batch runs |
| `beam` | Multiple style × code variants, ranked by critic score | Maximum quality |

### Human review loop

```bash
# 1. Run with --review to generate a feedback template
python cli.py plot --proposal paper.md --review

# 2. Edit run_dir/review.md — tag feedback to route it:
#    [style] Use Set2 palette        → re-runs style + code
#    [data]  Missing baseline         → re-runs explore + style + code
#    [code]  Use log scale            → re-runs code only

# 3. Resume with feedback applied
python cli.py plot --proposal paper.md --resume <run_dir> --review
```

## Scoring & Quality

**Statistical figures** (`plot`): Critic scores on 5 dimensions (max 10). Iterates until threshold:

```yaml
scoring:
  figure_score_threshold: 9.0    # accept if score >= this
  max_iterations: 3              # max attempts per figure
```

**Method diagrams** (`diagram`): Advocate scores on 6 dimensions (max 12):

```yaml
scoring:
  quality_thresholds:
    journal: 10.2
    conference: 9.6
    poster: 8.4
```

## Project Structure

```
HappyFigure/
├── cli.py                  # Entry point — CLI orchestrator
├── configs/
│   ├── pipeline.yaml       # Central config (LLM routing, scoring, agents)
│   ├── services.yaml       # Microservice config (SAM3, OCR, BEN2)
│   └── statistical_examples/  # Style few-shots for in-context learning
├── graphs/
│   ├── figure_pipeline.py     # Statistical figure StateGraph
│   └── svg_method_pipeline.py # SVG method drawing StateGraph (21 nodes)
├── agents/                 # Platform adapters (opencode, claude, codex, gemini, copilot)
├── llm/providers/          # LLM provider implementations
├── pipeline/               # Orchestration, execution strategies, feedback
├── prompts/agents/         # Agent prompt templates (source of truth)
├── scripts/pipeline_cli.py # CLI backend for agent tools
├── services/               # SAM3, OCR, BEN2 microservices
└── skills/                 # Claude Code skills (/plot, /diagram)
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Services won't start | Check `nvidia-smi` for GPU, ensure `happyfigure` conda env is active |
| `AZURE_OPENAI_API_KEY` not set | Set in `.env` file — see `.env.example` |
| Google Gemini 403/429 | Verify `GEMINI_API_KEY` or Vertex AI credentials |
| Agent exits immediately | Check logs in `runs/figure_runs/run_*/logs/` or `runs/diagram_runs/run_*/logs/` |
| `figure_code.py` import errors | Run `pip install -e ".[examples]"` for matplotlib/seaborn/scipy |
| Low critic scores (< 7) | Try `--execution beam` for multi-variant search |
| SVG validation exhausts retries | Simplify proposal or increase `scoring.max_iterations` in config |

## Documentation

| Document | What it covers |
|----------|---------------|
| [Configuration Guide](docs/CONFIGURATION.md) | Agent platforms, LLM presets, provider recipes, OpenCode auto-discovery |
| [Microservices Guide](docs/SERVICES.md) | SAM3/OCR/BEN2 setup, model weights, GPU requirements |
| [Claude Code Skills](docs/SKILLS.md) | `/plot`, `/diagram`, `/figures`, `/figure-planner` — when to use each |
| [LLM Routing](docs/LLM_ROUTING.md) | Deep dive into model routing across 4 config layers |
| [Onboarding Guide](docs/ONBOARD.md) | Step-by-step setup for specific environments (Vertex AI, Copilot, etc.) |
| [Cheatsheet](docs/cheatsheet.md) | Copy-paste commands for common tasks |
| [Image References](docs/REFERENCES.md) | Attribution for all reference images used in few-shot prompting |

## Privacy Note

HappyFigure sends your research proposal text and data descriptions to third-party LLM providers (OpenAI, Google, Anthropic, etc.) for figure generation. Review your data before running the pipeline to ensure no sensitive information is included.

## References & Acknowledgements

### Upstream Models & Services

| Project | Used for |
|---------|----------|
| [SAM3](https://github.com/facebookresearch/sam3) (Facebook Research) | Image segmentation — segments raster diagrams into bounding boxes for SVG reconstruction |
| [BEN2](https://github.com/PramaLLC/BEN2) (PramaLLC) | Background removal — clean transparent icon crops for SVG embedding |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) (Baidu) | Text detection — recognizes labels in raster images for editable SVG `<text>` elements |

### Reference Images

Architecture diagram examples in `configs/method_examples/` are from published papers and used under fair use for non-commercial research. See [docs/REFERENCES.md](docs/REFERENCES.md) for the full list with DOIs. Key sources:

- Xu et al., "A whole-slide foundation model for digital pathology from real-world data," *Nature* (2024) — [10.1038/s41586-024-07441-w](https://doi.org/10.1038/s41586-024-07441-w)
- Jumper et al., "Highly accurate protein structure prediction with AlphaFold," *Nature* (2021) — [10.1038/s41586-021-03819-2](https://doi.org/10.1038/s41586-021-03819-2)
- Ruffolo et al., "Designing CRISPR–Cas systems with language models," *Nature* (2025) — [10.1038/s41586-025-08817-w](https://doi.org/10.1038/s41586-025-08817-w)

### Code & Pipeline References

| Project | What we adapted |
|---------|----------------|
| [PaperBanana](https://github.com/dwzhu-pku/PaperBanana) | Proposal-to-figure generation concept; data exploration and planning stages |
| [AutoFigure-Edit](https://github.com/ResearAI/AutoFigure-Edit) | Raster-to-SVG conversion pipeline; SAM-based segmentation approach |
| [Paper2Any](https://github.com/OpenDCAI/Paper2Any) | Multi-format output architecture; LLM-driven code generation |
| [Edit-Banana](https://github.com/BIT-DataLab/Edit-Banana) | Iterative critic-driven refinement loop; multi-dimension scoring rubric |
| [figures4papers](https://github.com/ChenLiu-1996/figures4papers) | Style few-shot examples in `configs/statistical_examples/` and `configs/method_examples/` (see [image references](docs/REFERENCES.md)) |
| [claude-scientific-skills](https://github.com/K-Dense-AI/claude-scientific-skills) | Agent prompt engineering patterns for scientific figures |
| [auto-figures](https://github.com/SocraLee/auto-figures) | Multi-agent orchestration pattern |
| [pubfig](https://github.com/Galaxy-Dawn/pubfig) | Publication-ready plot type catalog; journal-aware styling conventions |

## License

MIT License. See [LICENSE](LICENSE) for details.
