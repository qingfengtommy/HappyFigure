# HappyFigure — Pipeline Execution Guide

## Entry Points & Execution Modes

All configuration values (thresholds, beam params, LLM routing) live in `configs/pipeline.yaml`.

### 1. `cli.py` — Entry Point

```
python cli.py <command> --proposal <file> [--results-dir <dir>] [flags]
```

| Command | Description |
|---------|-------------|
| `plot` | Statistical plots from experiment data |
| `diagram` | Method/architecture diagram (full SVG pipeline, requires services) |
| `composite` | Diagram + programmatic visualization compositing (4-agent, requires services) |
| `sketch` | Method diagram (lightweight, agent writes SVG directly, no services) |
| `paper` | Generate all figures for a paper (plots + diagrams + assembly) in one run |
| `review` | Interactively review figures from a completed run (writes `review.md`) |

| Global Flag | Effect |
|-------------|--------|
| `--agent {opencode\|claude\|codex\|gemini\|copilot}` | Agent runner CLI (overrides `pipeline.yaml agent.platform`) |
| `--llm-preset {azure\|gemini\|mixed}` | Override LLM config from `pipeline.yaml` |
| `--orchestrator-mode {agent-first\|python-stages}` | Orchestration mode (default from `pipeline.yaml`) |
| `--resume RUN_DIR` | Resume from a previous run directory (skips completed stages) |
| `--review` | Enable human review: generate `review.md` after run, consume on `--resume` |
| `--verbose` | Enable verbose logging |

### 2. `scripts/pipeline_cli.py` — Tool Backend for Agents

Called by OpenCode custom tools (`.opencode/tool/*.ts`). All output is JSON to stdout.

| Subcommand | Maps to |
|-----------|---------|
| `init` | Create run_dir + state.json from proposal |
| `data-scan` | Discover experiments, scan schemas |
| `data-process` | Run data_processor + execute_data_processing nodes |
| `figure-plan` | Planner → split → route (auto-bootstraps init+data-scan) |
| `figure-execute` | Stylist → code → execute → critic loop (max 3 iters) |
| `figure-execute-parallel` | All experiments in parallel (ThreadPoolExecutor) |
| `figure-execute-beam` | Beam search across style/code variants |
| `method-propose` | load_markdown → data_explorer → method_proposer |
| `svg-pipeline` | Full `app_svg_method.stream()` with per-node progress |
| `services` | Start/stop/health for SAM3, OCR, BEN2 microservices |

---

## Orchestration Modes

HappyFigure supports two orchestration modes, configured via `pipeline.yaml` `orchestrator.mode` or `--orchestrator-mode`:

### `agent-first` (default) — Main Orchestrator Agent

A single main agent (`happyfigure-orchestrator`) runs the entire pipeline in one session. It spawns subagents for bounded tasks and coordinates the three stages internally. Python is a thin launcher that starts services (if needed), launches the main agent, and syncs the manifest after completion.

```
Python (cli.py)
└─ launch_orchestrator_session("happyfigure-orchestrator", prompt)
   │
   Main orchestrator agent (single session)
   ├─ EXPLORE: spawn @data-explore → exploration_report.md
   │   └─ Main splits into per-experiment experiments/<exp>/description.md
   ├─ STYLE: main writes global_style.md + experiments/<exp>/styled_spec.md
   │   └─ (Beam) spawn @style-variant per variant, or write variants inline
   ├─ CODE:
   │   ├─ Sequential: main writes code, spawns @figure-critic
   │   ├─ Parallel: spawn @code-agent per experiment (concurrent via multi-tool-call)
   │   └─ Beam: spawn @code-agent per experiment×variant, main ranks and selects
   └─ Main writes summary
```

Parallel subagent spawning works because agent platforms (Claude Code, OpenCode, Codex, Gemini) execute multiple tool calls from a single LLM response concurrently. The main agent emits N Agent/subagent tool calls in one turn, and they run in parallel.

### `python-stages` (fallback) — Python Orchestrator

Python orchestrates three stages sequentially, spawning each agent as a subprocess. Preserved for debugging and as a fallback.

```
Python (cli.py → pipeline/orchestrator/main.py)
├─ stage_explore()  → spawn_subagent("data-explore" or "method-explore")
├─ stage_design()   → spawn_subagent("planner-stylist")
└─ stage_generate() → spawn_subagent("code-agent") per experiment
```

---

## Agent Orchestration — Plot Pipeline

### Three stages: Explore → Style → Code

**Stage 1 — Explore** (`@data-explore` subagent)
- Reads results directory + proposal
- Writes `exploration_report.md` + `exploration_summary.json`
- Main orchestrator splits into per-experiment `experiments/<exp>/description.md`
- Optional: `@code-explore` subagent for existing analysis scripts

**Stage 2 — Style** (main orchestrator or `@planner-stylist` subagent)
- Reads exploration report + proposal + style examples (`configs/statistical_examples/`)
- Writes `global_style.md` (rcParams, palette, fonts, spine rules)
- Writes `experiments/<exp>/styled_spec.md` per experiment (100+ lines, 13 sections)
- Writes `multi_figure_plan.md`
- (Beam) Writes variants: `experiments/<exp>/styled_spec_s{N}.md`

**Stage 3 — Code Generation** (varies by execution mode)

| Execution | Flow |
|-----------|------|
| `sequential` | Main writes code → executes → spawns `@figure-critic` → iterates (max 3) |
| `parallel` | Spawns `@code-agent` per experiment concurrently, each spawns `@figure-critic` internally |
| `beam` | Spawns `@code-agent` per experiment × style variant → main ranks → refines survivors |

- Archives each iteration: `figure_code_iter{N}.py`, `critic_result_iter{N}.json`
- Final figure promoted to `outputs/<exp>/figure.png`

### Beam Search (`--execution beam`)
- Stage 2 produces S style variants per experiment
- Stage 3 spawns S × C code-agent subagents per experiment
- Main ranks candidates by `(score, ACCEPT)`, keeps top `beam_width`
- Iteratively refines survivors with cumulative feedback history
- Feedback capped at `context.max_feedback_chars` (default 3000): last 2 rounds full, older rounds summarized

---

## Agent Orchestration — Diagram Pipeline

### Diagram Mode (`diagram`)

```
Main orchestrator agent
├─ EXPLORE: spawn @data-explore (optional) + read proposal
│   └─ Write method_description.md
├─ STYLE: write diagram_design_spec.md (layout, grouping, panels)
├─ CODE:
│   ├─ [services started by Python before agent launch]
│   ├─ Spawn @svg-builder → figure.png → SAM3/OCR/BEN2 → final.svg
│   └─ Spawn @svg-refiner → method_architecture.{svg,png}
```

Services (SAM3:8001, OCR:8002, BEN2:8003) are managed by Python — started before the main agent launch, stopped after.

### Composite Mode (`composite`)

Extends diagram with a 4th subagent for programmatic visualization replacement:

```
[Same as diagram, plus:]
├─ Spawn @viz-composer → discover tools → generate → evaluate → composite
```

### Sketch Mode (`sketch`)

Lightweight — no services, no raster generation:

```
Main orchestrator agent
├─ EXPLORE: read proposal → write method_description.md
├─ STYLE: write diagram_design_spec.md
└─ CODE: spawn @svg-author → SVG from text (self-review loop, max 3 iterations)
```

---

## Resume (`--resume RUN_DIR`)

Both orchestration modes support resuming from a prior run directory:

- **agent-first**: If the generate stage is already COMPLETED in the manifest, skips relaunching the main agent entirely. Otherwise relaunches (agent can see prior artifacts).
- **python-stages**: Skips each stage whose manifest record has COMPLETED status. Re-runs only missing stages. Beam variant specs are persisted in `beam_variant_specs.json` and reconstructed on resume.

---

## Human Review (`--review`)

Optional human-in-the-loop feedback. Without `--review`, the pipeline is unchanged.

### Workflow

```
# 1. Run with --review to generate a review template
python cli.py plot --proposal paper.md --review
→ produces run_dir/review.md (template with scores + issues per experiment)

# 2. Review interactively (alternative to manual editing)
python cli.py review <run_dir>
→ guided walkthrough: accept, give feedback, or open each figure
→ writes the same review.md

# 3. Edit review.md — tag feedback lines to route to the right stage
[style] Use Set2 palette             → re-runs style + code
[data]  Missing baseline results     → re-runs explore + style + code
[code]  Use log scale for y-axis     → re-runs code only
Untagged lines default to [code].

# 4. Resume with feedback applied
python cli.py plot --proposal paper.md --resume <run_dir> --review
→ parses review.md, invalidates affected stages, re-runs from earliest
```

### Feedback Files

On `--resume --review`, `parse_review()` writes per-stage files to `run_dir/feedback/`:

| File | Content | Consumed by |
|------|---------|------------|
| `feedback/human_style_feedback.md` | All `[style]` lines | `@planner-stylist` |
| `feedback/human_data_feedback.md` | All `[data]` lines | `@data-explore` |
| `feedback/human_code_feedback.md` | All `[code]` + untagged lines | `@code-agent` |
| `feedback/human_feedback_<exp>.md` | Per-experiment combined | `@code-agent` (scoped) |

Feedback file **paths** (not content) are injected into agent prompts (~30 tokens). Agents read files on demand.

### Style Preferences (cross-run)

`[style]` feedback is extracted into `configs/feedback/style_preferences.yaml` — a project-level file that persists across runs. Capped at 30 rules (oldest evicted). Loaded by the style stage on **every run**, even without `--review`.

```yaml
preferences:
  - "Use log scale for loss/perplexity metrics"
  - "Use Set2 palette for colorblind accessibility"
```

### Stage Routing

| Tag in `review.md` | Earliest re-run stage | What re-runs |
|----|----|----|
| `[data]` | explore | explore → style → code |
| `[style]` | design | style → code |
| `[code]` (or untagged) | generate | code only |

---

## Artifact Layout (v2)

```
run_dir/
├── run_manifest.json              # stage-oriented index (schema_version: 1)
├── state.json                     # runtime state (legacy, still used)
├── proposal.md
├── exploration_report.md          # from @data-explore
├── exploration_summary.json       # structured summary
├── global_style.md                # from style stage
├── multi_figure_plan.md           # from style stage
├── design_summary.json            # design stage index
├── experiments/
│   └── <experiment>/
│       ├── description.md         # per-experiment context
│       ├── styled_spec.md         # base styled spec
│       ├── styled_spec_s0.md      # beam variant 0
│       ├── styled_spec_s1.md      # beam variant 1
│       ├── figure_code.py         # latest code
│       ├── critic_result.json     # latest critic
│       ├── figure_code_iter1.py   # iteration archive
│       └── critic_result_iter1.json
├── outputs/
│   └── <experiment>/
│       ├── figure.png             # final promoted figure
│       ├── figure_code.py         # copy
│       └── critic_result.json     # copy
├── review.md                      # human review template (--review)
├── feedback/                      # parsed review feedback (--resume --review)
│   ├── human_style_feedback.md
│   ├── human_data_feedback.md
│   ├── human_code_feedback.md
│   └── human_feedback_<exp>.md
├── debug/                         # beam candidates, iteration archives
└── logs/
    ├── happyfigure-orchestrator.log
    └── code-agent_<exp>.log
```

### Agents

| Agent | Role | Spawned by |
|-------|------|------------|
| `happyfigure-orchestrator` | Main session — coordinates all stages | Python (agent-first mode) |
| `data-explore` | Scan data files, schemas, experiments | Orchestrator |
| `code-explore` | Read existing analysis scripts (optional) | Orchestrator |
| `planner-stylist` | Plan figures, write styled specs | Orchestrator or Python |
| `style-variant` | Produce beam-search style variants | Orchestrator |
| `code-agent` | Generate figure code, iterate with critic | Orchestrator or Python |
| `figure-critic` | 5-dimension quality scoring (subagent) | code-agent |
| `method-explore` | Explore method/architecture for diagrams | Orchestrator or Python |
| `svg-author` | Write SVG directly (sketch mode) | Orchestrator |
| `svg-builder` | Generate SVG via SAM3/OCR/BEN2 pipeline | Orchestrator or Python |
| `svg-refiner` | Refine SVG element-by-element | Orchestrator or Python |
| `viz-composer` | Replace raster viz with programmatic versions | Orchestrator or Python |

Agent prompts live in `prompts/agents/<name>.md`. Platform adapters generate platform-specific config files (`.claude/agents/`, `.opencode/agent/`, etc.) from these prompts during `setup()`.

---

## Figure Pipeline (`graphs/figure_pipeline.py`)

Sequential LangGraph app for statistical figure generation. Parallel and beam execution are handled by `pipeline_cli.py` subcommands using the same nodes.

### `app` (sequential)
```
pre_data_explorer → load_input → data_processor → execute_data_processing
  → data_explorer → figure_planner → split_plan → route_figures
    → [statistical/visualization] stylist → code_agent → execute_code → critic
    → [multi_panel] multi_panel → critic
  → critic routes: continue loop (max 3 iters) OR next_experiment
  → next_experiment routes: next exp's route OR END
```

---

## SVG Method Drawing Pipeline (`graphs/svg_method_pipeline.py`)

# SVG Method Drawing Pipeline — Diagnostic Tool Design

## Pipeline Structure (21 nodes)

```
load_markdown → method_data_explorer → method_proposer → image_generation
  → [error? → fail_end]
  → sam3_detect → sam3_review → sam3_merge_classify
    → ocr_text_detection → icon_extraction
    → visualization_code_gen → svg_generation → svg_validation
      → [valid? → icon_replacement]
      → [invalid + retries left? → svg_fix → svg_validation]  (fix loop, max 3)
      → [exhausted? → fail_end]
      → icon_replacement → svg_render → advocate_review
          → [accept → finalize → END]
          → [refine → svg_refinement → svg_validation]  (review loop, max 3)
          → [regenerate → regenerate_prompt → image_generation]  (regen loop)
```

### Three iteration loops:
1. **SVG fix loop**: svg_validation ↔ svg_fix (max 3 iterations)
2. **Review loop**: svg_render → advocate_review → refine → svg_refinement → svg_validation (max 3 iterations)
3. **Regeneration loop**: advocate_review → regenerate_prompt → image_generation → SAM3 → ... (re-enters full pipeline)

## Node I/O Reference (key state fields)

| Node | Key Inputs | Key Outputs | Visual Artifact |
|------|-----------|-------------|-----------------|
| `load_markdown` | `input_dir` | `proposal`, `run_dir`, `architecture_few_shots` | — |
| `method_data_explorer` | `results_dir`, `proposal` | `data_exploration_report` | — |
| `method_proposer` | `proposal`, `data_exploration_report`, `architecture_few_shots` | `method_description` | — |
| `image_generation` | `method_description`, `run_dir` | `generated_image_path`, `drawing_prompt` | `figure.png` |
| `sam3_detect` | `generated_image_path` | `sam_stage1_prompts`, `sam_stage1_results`, `sam_stage1_overlay_path` | `sam_stage1_overlay.png` |
| `sam3_review` | `sam_stage1_overlay_path`, `sam_stage1_results`, `method_description` | `sam_stage2_prompts`, `sam_stage2_results` | `sam_stage2_results.json` |
| `sam3_merge_classify` | `sam_stage1_results`, `sam_stage2_results`, `generated_image_path` | `samed_image_path`, `valid_boxes`, `boxlib_path` | `samed.png` |
| `ocr_text_detection` | `generated_image_path`, `valid_boxes` | `valid_boxes` (updated w/ `ocr_text`) | — |
| `icon_extraction` | `generated_image_path`, `valid_boxes` | `icon_infos`, `visualization_icons` | icon PNGs in run_dir |
| `visualization_code_gen` | `icon_infos` | `icon_infos` (updated `nobg_path`) | generated viz PNGs |
| `svg_generation` | `generated_image_path`, `samed_image_path`, `valid_boxes`, `method_description` | `svg_code`, `template_svg_path` | `template.svg` |
| `svg_validation` | `svg_code` | `svg_valid`, `svg_errors` | — |
| `svg_fix` | `svg_code`, `svg_errors` | `svg_code`, `svg_fix_iteration` | `fixed_vN.svg` |
| `icon_replacement` | `svg_code`, `icon_infos`, `generated_image_path` | `svg_code`, `final_svg_path`, `scale_factors` | `final.svg` |
| `svg_render` | `final_svg_path` | `figure_path` | `method_architecture_vN.png` |
| `advocate_review` | `figure_path`, `method_description`, `svg_code`, `doc_type` | `advocate_feedback`, `refinement_action`, `combined_score` | `advocate_review_vN.json` |
| `svg_refinement` | `svg_code`, `advocate_feedback`, images | `svg_code`, `final_svg_path` | `refined_vN.svg`, `composite_for_refinement.png` |
| `regenerate_prompt` | `method_description`, `advocate_feedback` | `refined_prompt` | — |
| `finalize` | `run_dir`, `final_svg_path`, `figure_path` | `success`, `figure_paths` | `method_architecture.{svg,png}`, `review_log.json` |
| `fail_end` | `svg_code`, `svg_errors`, `error` | `success=False`, `error` | — |

## Review Feedback Structure

**Advocate** evaluates 6 dimensions (0-2 each, total 0-12):
- clarity, label_readability, visual_hierarchy, ambiguity_risks, publication_readiness, visual_fidelity

Score out of 12. Thresholds in `configs/pipeline.yaml`: journal: 10.2, conference: 9.6, poster: 8.4.

## Advocate Routing Logic
1. If review has `review_skipped` or `parse_failure` AND iterations remain → **refine** (never accept fabricated scores)
2. `score >= threshold` OR `iteration >= max` → **accept** (tagged `unreviewed_final` if review unreliable)
3. `structural issues detected` → **regenerate** (back to image_generation)
4. else → **refine** (SVG refinement with LLM)

Thresholds configured in `configs/pipeline.yaml` (`scoring.quality_thresholds`).

## Artifacts in run_dir

```
run_dir/
  figure.png                      # original raster from Gemini
  samed.png                       # SAM3 annotated boxes
  boxlib.json                     # box coordinates
  template.svg                    # initial SVG
  final.svg                       # after icon replacement
  method_architecture.svg         # canonical final SVG
  method_architecture.png         # canonical final render
  method_architecture_vN.png      # per-iteration renders
  advocate_review_vN.json         # advocate feedback
  review_log.json                 # summary with scores
  refined_vN.svg                  # refined SVGs
  composite_for_refinement.png    # 3-panel feedback image
  method_description.md           # method proposer output
  method_data_exploration_report.md
  prompt_<node>_<suffix>.md       # node prompt inputs (verbose mode)
```

## Diagnostic Tool Requirements

Goal: step-by-step pipeline inspector that shows input/output/visualization for each node.

### Design: `scripts/pipeline_inspector.py`

```
python scripts/pipeline_inspector.py <run_dir> [--node <node_name>] [--html] [--diff]
```

**Features:**
1. **Full run summary**: list all nodes that executed, scores, iteration counts
2. **Per-node inspection**: show text inputs, text outputs, and render any visual artifacts
3. **Diff mode**: side-by-side comparison between iterations (template vs optimized vs refined)
4. **Review inspector**: formatted architect/advocate feedback with dimension breakdown
5. **HTML report**: single-page HTML with embedded images for sharing

### Data sources per node:
- **Text I/O**: reconstruct from `state.json` snapshot (needs pipeline to save state after each node)
- **Visual artifacts**: already saved as PNG/SVG in run_dir
- **LLM prompts/responses**: from `prompt_<node>_<suffix>.md` files in run_dir (verbose mode only)

### Key implementation steps:
1. Add state snapshot saving after each node in the pipeline (or use existing verbose logs)
2. Build inspector CLI that reads run_dir artifacts
3. Per-node renderers that format inputs/outputs appropriately
4. HTML report generator with embedded base64 images

---

## Microservices

The SVG method pipeline requires three microservices (managed by `pipeline_cli.py services`):

| Service | Port | Purpose | Client |
|---------|------|---------|--------|
| SAM3 | 8001 | Image segmentation (14 prompt types) | `services.sam3.client` |
| PaddleOCR | 8002 | Text detection in images | `services.ocr.client` |
| BEN2 | 8003 | Background removal for icons | `services.ben2.client` |

Service config: `configs/services.yaml`. BEN2 endpoint also configurable via `BEN2_SERVICE_URL` env var or `configs/pipeline.yaml`.

## Centralized Configuration (`configs/pipeline.yaml`)

Key pipeline parameters are centralized in `configs/pipeline.yaml`, loaded by `graphs.svg_utils.load_pipeline_config()` (thread-safe, cached, graceful fallback to `{}`). Some CLI defaults in `cli.py` still have local fallbacks.

| Section | Key values |
|---------|-----------|
| `orchestrator.mode` | `agent-first` (default) or `python-stages` |
| `scoring.quality_thresholds` | Per-doc-type thresholds (journal: 10.2, conference: 9.6, etc.) |
| `scoring.figure_score_threshold` | Figure critic accept threshold (default 9.0) |
| `scoring.max_iterations` | Max code/review iterations (default 3) |
| `context.initial_prompt_budget` | Token cap for Python-built task prompts (default 30000) |
| `context.max_feedback_chars` | Beam feedback history cap (default 3000) |
| `context.max_bundled_lines` | Inline vs path-ref threshold (default 200) |
| `ocr.confidence_threshold` | OCR detection confidence floor (default 0.6) |
| `services.ben2_endpoint` | BEN2 service URL (default `http://127.0.0.1:8003`) |
| `svg.render_scale` | SVG→PNG render scale (default 2.0) |
| `svg.label_font_min/max` | Dynamic label font range (12–48) |
| `svg.overlap_ratio_threshold` | Text overlap detection (0.3) |
| `svg.oversized_font_threshold` | Font sanity check limit (default 60px) |
| `beam.width/style_variants/code_variants/iterations` | Beam search parameters |
| `image_generation.size` | Image gen dimensions (default "1024x1024") |
| `viz_composer.accept_threshold` | Score threshold to accept programmatic viz (default 6/8) |
| `viz_composer.min_accuracy/min_fidelity` | Minimum dimension scores (default 1/2 each) |
| `viz_composer.max_retries_per_script` | Max retries for viz generation scripts (default 2) |
| `viz_composer.tool_preferences` | Per-viz-type rendering tool mapping |
| `feedback.max_style_preferences` | Cap on project-level style rules (default 30) |

## Key Source Files

| File | Purpose |
|------|---------|
| `cli.py` | Entry point: subcommand CLI, context setup, launches orchestrator |
| `pipeline/orchestrator/main.py` | Top-level driver: agent-first vs python-stages dispatch |
| `pipeline/orchestrator/artifacts.py` | Canonical v2 artifact paths (single source of truth) |
| `pipeline/orchestrator/steps.py` | Stage execution: explore, design, generate, assemble (paper), resume |
| `pipeline/orchestrator/strategies.py` | Pluggable execution handlers (sequential/parallel/beam) |
| `pipeline/orchestrator/modes.py` | CLI command → mode resolution |
| `pipeline/agent_runtime.py` | Agent subprocess execution, doom-loop detection |
| `pipeline/contracts.py` | Typed stage contracts: ExplorationResult, DesignResult, StageRecord |
| `pipeline/prompt.py` | Priority-ordered prompt composition with budget control |
| `pipeline/plot_planning.py` | Planner-stylist prompt building, spec validation |
| `pipeline/plot_execution.py` | Code-agent execution (sequential + parallel) |
| `pipeline/feedback.py` | Human review: template gen, parsing, preferences, interactive CLI |
| `pipeline/plot_beam.py` | Beam search: style variants, ranking, refinement |
| `pipeline/drawing.py` | SVG/diagram/sketch/composite step implementations |
| `orchestrator/__init__.py` | Agent platform registry and base class |
| `scripts/pipeline_cli.py` | CLI backend for OpenCode tools (JSON I/O) |
| `graphs/figure_pipeline.py` | Figure generation StateGraph (`app`) |
| `graphs/svg_method_pipeline.py` | SVG method drawing StateGraph (`app_svg_method`, 21 nodes) |
| `graphs/svg_utils.py` | Shared utils: SVG validation, config loader, prompt loader, rendering |
| `configs/pipeline.yaml` | Centralized pipeline configuration |
| `configs/services.yaml` | Microservice config (SAM3, OCR, BEN2) |
