# Claude Code Skills

HappyFigure ships with two built-in [Claude Code skills](https://docs.anthropic.com/en/docs/claude-code/skills) (`skills/`) and also works with two global Claude Code skills (`/figures`, `/figure-planner`).

## Available Skills

| Skill | Type | What it does | Services needed |
|-------|------|-------------|-----------------|
| `/plot` | Repo | Launches the HappyFigure multi-agent pipeline for statistical figures | No |
| `/diagram` | Repo | Launches the HappyFigure pipeline for method/architecture diagrams | `diagram` mode only |
| `/figures` | Global | Standalone — writes and executes Python plotting scripts directly (no HappyFigure pipeline) | No |
| `/figure-planner` | Global | Planning-only — scans data, outputs a structured `paper_summary.md` with plot-type specs per panel | No |

## When to use `/plot` vs `/figures`

| | `/plot` (HappyFigure pipeline) | `/figures` (standalone) |
|---|---|---|
| **How it works** | 3 specialized agents in sequence (explore → plan → code + critic loop) | Single Claude Code session writes scripts directly |
| **Quality control** | Automated critic scores each figure on 5 dimensions; iterates until 9.0/10 | Self-review by the same agent; manual user feedback |
| **Style consistency** | Few-shot examples from `configs/statistical_examples/` | Built-in publication style rules (Nature/Cell/NeurIPS) |
| **Execution modes** | Sequential, parallel (4 threads), beam search | Sequential only |
| **Best for** | Batch generation from a proposal; reproducible runs; max quality | Quick one-off plots; interactive iteration; complex data exploration |
| **Setup** | HappyFigure installed + agent platform configured | Works anywhere Claude Code runs |

**Rule of thumb**: `/plot` for automated, critic-scored figures at scale. `/figures` for hands-on control or when HappyFigure isn't set up.

You can combine them: run `/figure-planner` first to get a structured spec, then feed it as the proposal to `/plot`.

## When to use `sketch` vs `diagram`

| | `sketch` | `diagram` |
|---|---|---|
| **Pipeline** | 2 agents: method-explore → svg-author | 3 agents + image API + microservices |
| **How SVG is created** | Agent writes SVG directly from text | Generates raster image → SAM3 segmentation → SVG reconstruction → refinement |
| **Visual fidelity** | Clean vectors; depends on agent's SVG skill | Higher fidelity — preserves spatial layout and icons from raster |
| **Review** | Self-review loop (max 3 iterations) | Advocate reviewer scores 6 dimensions with iterative refinement |
| **Services** | None | SAM3 + OCR + BEN2 (needs GPU) |
| **Runtime** | Minutes | Slower (image gen + 3 microservice passes + multi-round review) |
| **Best for** | Drafts, rapid iteration, simple flowcharts | Camera-ready figures, complex architectures, raster-to-SVG conversion |

**Rule of thumb**: Start with `sketch` for speed. Upgrade to `diagram` for publication quality or fine-grained icon detail.
