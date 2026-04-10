---
name: plot
description: >-
  Generate publication-ready statistical plots and charts from experiment data
  using the HappyFigure multi-agent pipeline. Use this skill whenever the user
  wants to create figures, charts, plots, or statistical visualizations from
  experimental results — including phrases like "plot my results", "make a bar
  chart", "generate figures for my paper", "visualize the data", "I need
  results figures", "show performance comparison", or any request to turn
  experiment data into publication-quality graphics. Also triggers for requests
  about rerunning or improving previously generated figures.
---

# Plot — Statistical Figure Generation

You are helping the user generate publication-ready statistical figures from
experiment data. The pipeline is fully automated — your job is to gather the
right inputs, pick the right settings, and launch it.

## What you need from the user

Before running anything, make sure you have these two things:

1. **A proposal file** (markdown) — describes what figures to create, what the
   paper is about, what experiments were run. This is required.
2. **A results directory** — where the experiment data lives. Defaults to
   `./results` if not specified. Ask the user if you're unsure where their data is.

If the user doesn't have a proposal file, help them create one — it should
describe the paper's goal, the experiments, and what figures they want.

## Deciding how to run it

### Execution strategy

Pick based on what the user needs:

- **`sequential`** (default) — runs one experiment at a time. Fine for most
  cases, especially with 1-3 experiments. Simplest and most predictable.
- **`parallel`** — runs up to 4 experiments concurrently. Use when there are
  many experiments and the user wants speed over debuggability.
- **`beam`** — explores multiple style and code variants, ranks by critic
  score, and keeps the best. Use when quality matters more than speed — for
  example, camera-ready figures or when the user says "give me the best
  possible figure". This takes significantly longer but produces higher quality.

### Agent platform and LLM preset

Usually leave these at their defaults (from `configs/pipeline.yaml`). Only
override if the user specifically asks:

- `--agent {opencode|claude|codex|gemini}` — which CLI tool runs the agents
- `--llm-preset {azure|gemini|mixed}` — which LLM providers to use

## Running the pipeline

```bash
python cli.py plot --proposal <path> [--results-dir <dir>] [options]
```

The pipeline runs three sequential agent steps:

```
Step 1: data-explore agent     → reads proposal + data, writes exploration report + summary JSON
Step 2: planner-stylist agent  → plans figures, writes styled specs per experiment
Step 3: code-agent (per exp)   → generates Python figure code, executes, iterates with critic
```

Each code-agent iteration generates matplotlib/seaborn code, runs it, and
invokes a figure-critic subagent that scores on 5 dimensions (max 10). It
iterates until the score meets the threshold (default 9.0) or hits max
iterations (default 3). Both are configurable in `configs/pipeline.yaml`.

### Common invocations

```bash
# Most common: generate figures from a proposal
python cli.py plot --proposal paper.md

# User has data in a specific directory
python cli.py plot --proposal paper.md --results-dir ./experiment_results

# User wants the best quality (camera-ready, final submission)
python cli.py plot --proposal paper.md --execution beam

# User wants speed with many experiments
python cli.py plot --proposal paper.md --execution parallel
```

### Beam search options (only relevant with `--execution beam`)

Beam search generates multiple style variants × code variants, scores them all,
and keeps the top candidates. Defaults work well, but the user can tune:

| Flag | Default | What it does |
|------|---------|-------------|
| `--beam-width` | 2 | How many candidates survive each round |
| `--style-variants` | 2 | Number of different visual styles to try |
| `--code-variants` | 2 | Code implementations per style |
| `--beam-iterations` | 2 | Rounds of refinement |

### Other options

Scoring and iteration limits are configured in `configs/pipeline.yaml`.

## Output

All outputs land in `runs/figure_runs/run_<timestamp>/`. Tell the user where
to find their figures — the key files are:

- `<experiment>/figure_code.py` — the final Python code
- `<experiment>/*.png` — the generated figure images
- `<experiment>/critic_result.json` — quality scores and feedback
- `multi_figure_plan.md` — the overall figure plan

If something went wrong, check `logs/orchestrator.log` and
`logs/agent_*.log` in the run directory.

## When this is NOT the right skill

- If the user wants a **method/architecture diagram** (not a data plot), use the
  `diagram` or `sketch` command instead — those are for structural diagrams
  showing how a system works, not for visualizing experimental results.
- If the user wants to **edit an existing figure's code** rather than regenerate
  from scratch, just help them edit the Python file directly.
