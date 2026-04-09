# HappyFigure

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

AI-powered scientific figure generation pipeline. HappyFigure takes a research proposal and experimental results, then automatically generates publication-ready statistical figures and method/architecture diagrams.

## Features

- **Statistical figures**: Bar charts, line plots, heatmaps, scatter plots, multi-panel compositions
- **Architecture diagrams**: SVG method drawings from text descriptions, with optional image segmentation
- **Multi-provider LLM support**: Azure, Google Gemini, OpenAI, Anthropic, AWS Bedrock
- **Agent platforms**: Run via OpenCode, Claude Code, Codex CLI, or Gemini CLI
- **Configurable pipeline**: All parameters in `configs/pipeline.yaml`
- **Iterative refinement**: Critic-driven loop scores figures on a 5-dimension rubric

## Installation

> **Note**: The original `requirements-static.txt` is a full pip-freeze (411 packages) from the
> author's environment and should NOT be used directly — it will downgrade your PyTorch/CUDA stack.
> Use the steps below instead.

### Prerequisites

- Python 3.10–3.12 (recommended: 3.12). Python 3.13 works for core features but has no PaddleOCR wheel.
- For the `[svg-fallback]` extra: `sudo apt install libcairo2-dev pkg-config`
- For GPU-accelerated microservices: NVIDIA GPU with CUDA drivers

### Step 1: Create environment (recommended)

```bash
conda create -n happyfigure python=3.12 -y
conda activate happyfigure
```

### Step 2: Install dependencies

```bash
cd HappyFigure

# Dependency-only install for running from this repo checkout
pip install -r requirements.txt

# Or install HappyFigure itself into the environment
pip install -e .

# Optional extras on top of the package install
pip install -e ".[google]"        # Google Gemini (API key or Vertex AI)
pip install -e ".[azure]"         # Add Azure CLI / Managed Identity auth support
pip install -e ".[anthropic]"     # Anthropic Claude
pip install -e ".[all]"           # All LLM providers + SVG + examples
```

`pip install -r requirements.txt` only installs dependencies. Use `pip install -e .` if you want
HappyFigure installed as a package with packaged `configs/` and `prompts/`.

The default `pipeline.yaml` routes `chat` / `code` / `drawing` to the `azure` provider. That provider
supports either:
- Standard Azure OpenAI: `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY`

Install `.[azure]` only if you want automatic Azure CLI / Managed Identity token acquisition.

### Step 3: Install optional extras (as needed)

```bash
pip install -e ".[svg]"            # cairosvg + lxml for SVG rendering
pip install -e ".[examples]"       # seaborn, scipy for running config examples
```

### Step 4: Microservices (only for `diagram` command)

The `diagram` command needs SAM3/OCR/BEN2 on ports 8001-8003. The `plot` and `sketch` commands do NOT need these.

```bash
# PyTorch >=2.6 from PyPI ships with CUDA by default.
# For a specific CUDA version, install torch first:
#   pip install torch --index-url https://download.pytorch.org/whl/cu126

pip install -e ".[services]"
```

Model weights (~3 GB total) are **auto-downloaded on first service launch** — no manual download needed. See [Model Weights](#sam3-model-weights) for details and offline setup.

### Step 5: Development

```bash
pip install -e ".[dev]"
```

### Optional: Custom pipeline config overlay

Override specific `pipeline.yaml` settings without modifying the original:
```bash
export HAPPYFIGURE_CONFIG=configs/pipeline_override.yaml
```
See `configs/pipeline_override.yaml` for an example.

## Quick Start

### 1. Configure LLM provider

Copy and edit the environment file:
```bash
cp .env.example .env
# Set your API key(s):
# GEMINI_API_KEY=your-key
# AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
# AZURE_OPENAI_API_KEY=your-key
#
# Or for Vertex AI (no API key needed):
# GOOGLE_CLOUD_PROJECT=your-project-id
# GOOGLE_CLOUD_LOCATION=global
# GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Configure per-role model routing in `configs/pipeline.yaml` (see LLM Configuration below).
Configured `model` values are passed through to the provider for each role.

### 2. Choose an agent platform

HappyFigure delegates interactive work (data exploration, figure coding, SVG drawing) to an **agent platform** — an AI coding assistant that runs shell commands and edits files on your behalf.

| Platform | CLI flag | Notes |
|----------|----------|-------|
| **OpenCode** (default) | `--agent opencode` | Requires `.opencode/opencode.jsonc` — copy from `.opencode/opencode.jsonc.example` and configure your provider |
| **Claude Code** | `--agent claude` | Uses the `claude` CLI; requires Anthropic API key |
| **Codex** | `--agent codex` | Uses the `codex` CLI; requires OpenAI API key |
| **Gemini CLI** | `--agent gemini` | Uses the `gemini` CLI; requires Google API key |

The default platform is set in `configs/pipeline.yaml` under `agent.platform`. Override per-run with `--agent`.

### 3. Run the pipeline

Every command takes two key flags: `--agent` (who drives the work) and `--llm-preset` (which LLM provider the pipeline nodes use). They are independent — you can mix any agent with any LLM backend.

```bash
# Pick your agent platform + LLM provider
python cli.py plot --agent opencode --llm-preset gemini \
    --proposal configs/example_proposal.txt --execution parallel

# Same command with OpenCode agent + Azure LLM (the defaults)
python cli.py plot --proposal configs/example_proposal.txt

# Architecture diagram (full pipeline: image gen → SAM3 → SVG → review)
python cli.py --agent claude diagram --proposal configs/example_proposal.txt

# Quick architecture diagram (agent writes SVG directly, no microservices)
python cli.py --agent codex sketch --proposal configs/example_proposal.txt

# Override just the LLM provider
python cli.py plot --proposal paper.md --llm-preset gemini
```

All commands default `--results-dir` to `./results`.

### 4. Find your figures

`plot` output goes to `notes/figure_runs/run_YYYYMMDD_HHMMSS/`.
Method runs (`sketch`, `diagram`, `composite`) go to `notes/diagram_runs/run_YYYYMMDD_HHMMSS/`.

## Commands

| Command | Description |
|---------|-------------|
| `plot` / `figure` | Statistical plots (bar, line, heatmap, etc.) from experiment data |
| `diagram` / `method` | Architecture/method diagram via full pipeline: image generation → SAM3 segmentation → SVG conversion → advocate review. Requires SAM3/OCR/BEN2 microservices |
| `sketch` / `method-svg` | Architecture/method diagram written directly as SVG by an LLM agent. Faster and needs no microservices |
| `composite` / `hybrid` | Method diagram plus programmatic visualization compositing |

`plot` is now stats-only. `--scope statistical` is the normal value, and `--scope full` is kept only as a deprecated compatibility alias that maps to the same behavior.

## Architecture

HappyFigure has **two independent LLM integration layers** that you can mix and match:

```
┌────────────────────────────────────────────────────────┐
│                    cli.py                          │
│                   (orchestrator)                        │
├─────────────────────┬──────────────────────────────────┤
│  Agent Platform     │  LLM Providers (pipeline nodes)  │
│  ─────────────────  │  ──────────────────────────────  │
│  Drives multi-step  │  Direct API calls for specific   │
│  reasoning tasks:   │  pipeline tasks:                 │
│   • opencode        │   • chat  (text generation)      │
│   • claude          │   • code  (code generation)      │
│   • codex           │   • drawing (image generation)   │
│   • gemini          │                                  │
│                     │  Providers:                      │
│  Configured in:     │   • Azure-compatible OpenAI      │
│  pipeline.yaml      │   • Google Gemini / Vertex AI    │
│  agent: section     │   • OpenAI                       │
│                     │   • Anthropic                    │
│                     │   • AWS Bedrock                  │
│                     │                                  │
│                     │  Configured in:                  │
│                     │  pipeline.yaml llm: section      │
└─────────────────────┴──────────────────────────────────┘
```

- **Agent platform** = which CLI tool runs multi-step reasoning (explores data, plans figures, writes code, critiques results). You choose one platform; all agents for that run use the same model.
- **LLM providers** = which APIs the pipeline nodes call directly for text/code/image generation. Configured per-role (`chat`, `code`, `drawing`).

These are **independent**: you can use OpenCode agents running Gemini models while using OpenAI for image generation.

### Agent Flow per Command

**`plot`** — 3 agents run sequentially:

| Step | Agent | Role |
|------|-------|------|
| 1 | `data-explore` | Explores data files, writes exploration report + summary JSON |
| 2 | `planner-stylist` | Plans and styles figures per experiment |
| 3 | `code-agent` (+`figure-critic` subagent) | Generates matplotlib code, iterates until score threshold |

**`diagram`** — 3 agents + image generation API + microservices:

| Step | Agent | Role |
|------|-------|------|
| 1 | `method-explore` | Reads proposal, writes method description |
| 2 | `svg-builder` | Generates raster image (uses `drawing` LLM role), runs SAM3, builds SVG |
| 3 | `svg-refiner` | Iteratively improves SVG with advocate reviewer scoring |

**`composite`** — 4 agents, extends `diagram` with programmatic visualization compositing:

| Step | Agent | Role |
|------|-------|------|
| 1 | `method-explore` | Reads proposal, writes method description (with Visualization Panels section) |
| 2 | `svg-builder` | Generates raster image, runs SAM3, builds SVG with `viz_type` metadata |
| 3 | `svg-refiner` | Iteratively improves SVG with advocate reviewer scoring |
| 4 | `viz-composer` | Replaces raster visualization regions (bar charts, heatmaps, etc.) with programmatically generated versions |

Steps 1-3 require SAM3/OCR/BEN2 microservices; step 4 runs after services are stopped. Use `--skip-viz-compose` to run only steps 1-3.

**`sketch`** — 2 agents, no services needed:

| Step | Agent | Role |
|------|-------|------|
| 1 | `method-explore` | Reads proposal, writes method description |
| 2 | `svg-author` | Writes SVG directly with self-review loop (max 3 iterations) |

> **Key insight**: The `drawing` LLM role (image generation API) is **only used by `diagram` and `composite`**. The `plot` and `sketch` commands only use agents — no direct LLM provider calls.

### Project Structure

```
HappyFigure/
├── cli.py                  # Entry point — CLI orchestrator
├── configs/
│   ├── pipeline.yaml            # Central config (LLM routing, scoring, agents)
│   ├── services.yaml            # Microservice config (SAM3, OCR, BEN2)
│   ├── method_examples/         # Reference SVG diagrams for sketch mode
│   └── statistical_examples/    # Style few-shots for statistical plots
├── llm/
│   ├── __init__.py              # Config-driven LLM router
│   └── providers/               # Provider implementations
│       ├── azure_provider.py
│       ├── google_provider.py
│       ├── openai_provider.py
│       ├── anthropic_provider.py
│       └── bedrock_provider.py
├── graphs/
│   ├── figure_pipeline.py       # Statistical figure StateGraph
│   ├── svg_method_pipeline.py   # SVG method drawing StateGraph (21 nodes)
│   └── svg_utils.py             # Shared utilities (validation, rendering)
├── agents/
│   ├── __init__.py              # Platform registry + base class
│   ├── opencode.py              # OpenCode adapter
│   ├── claude_code.py           # Claude Code adapter
│   ├── codex.py                 # Codex adapter
│   └── gemini.py                # Gemini CLI adapter
├── prompts/
│   └── agents/                  # Agent prompt templates (source of truth)
├── scripts/
│   └── pipeline_cli.py          # CLI backend for agent tools
├── services/
│   ├── sam3/                     # SAM3 microservice
│   ├── ocr/                      # PaddleOCR microservice
│   └── ben2/                     # BEN2 microservice
```

## Configuration

All configuration lives in **`configs/pipeline.yaml`**. There are two main sections:

### Agent Platform (`agent:` section)

Controls which CLI tool runs the agents and what model they use:

```yaml
agent:
  platform: opencode          # default platform
  opencode:
    model: gpt-5.4            # model name for this platform
    provider: azure             # provider name for this platform
  claude:
    model: claude-opus-4.6
  codex:
    model: gpt-5.4
    sandbox_mode: workspace-write
    retry_dangerous_on_sandbox_failure: true
  gemini:
    model: gemini-2.5-pro
```

Override at runtime:

```bash
python cli.py plot --proposal paper.md --agent gemini
```

| Platform | Flag | CLI tool needed | Status |
|----------|------|-----------------|--------|
| OpenCode | `--agent opencode` | [`opencode`](https://github.com/sst/opencode) | Stable (default) |
| Claude Code | `--agent claude` | [`claude`](https://docs.anthropic.com/en/docs/claude-code) | Stable |
| Codex | `--agent codex` | [`codex`](https://github.com/openai/codex) | Experimental |
| Gemini CLI | `--agent gemini` | [`gemini`](https://github.com/google-gemini/gemini-cli) | Experimental |

The orchestrator reads `agent.<platform>.model` and `agent.<platform>.provider`, then generates agent config files with that model for the selected platform. Agent prompt templates are in `prompts/agents/`.

For Codex, `sandbox_mode` controls the normal launch mode. If Codex exits with a known Linux bubblewrap startup error, `retry_dangerous_on_sandbox_failure: true` makes the orchestrator retry once with `danger-full-access`. This is useful in restricted container or AppArmor environments, but it deliberately trades away Codex's own sandbox for that retry.

#### OpenCode Provider Auto-Discovery

OpenCode has [75+ built-in providers](https://opencode.ai/docs/providers/) (GitHub Copilot, Google, Anthropic, OpenAI, and many more). For these, **no configuration is needed in `opencode.jsonc`** — models are auto-discovered once you authenticate:

```bash
# One-time setup: authenticate with a built-in provider
opencode          # then run /connect and select your provider (e.g., GitHub Copilot)

# See all available models across connected providers
opencode          # then run /models
```

After connecting, you can use any model from that provider directly in `pipeline.yaml`:

```yaml
agent:
  opencode:
    model: gemini-2.5-pro             # model ID from the provider
    provider: github-copilot           # built-in provider name
```

The model string `github-copilot/gemini-2.5-pro` is written into each agent's frontmatter and OpenCode resolves it automatically.

**When do you need a custom provider in `opencode.jsonc`?** Only for non-standard endpoints that OpenCode doesn't ship with — for example, a custom Azure OpenAI deployment:

```jsonc
// .opencode/opencode.jsonc — only custom providers need config
{
  "provider": {
    "azure": {
      "npm": "@ai-sdk/openai",
      "options": { "baseURL": "https://your-resource.openai.azure.com/openai/v1" },
      "models": { "gpt-5.4": { "id": "gpt-5.4_2026-03-05" } }
    }
    // Built-in providers (github-copilot, google, anthropic, openai, etc.)
    // do NOT need entries here — just /connect to authenticate.
  }
}
```

### LLM Providers (`llm:` section)

Controls which API is called for each pipeline role:

```yaml
llm:
  providers:
    azure:
      api_key_env: AZURE_OPENAI_API_KEY   # also set AZURE_OPENAI_ENDPOINT
    google:
      api_key_env: GEMINI_API_KEY
    # openai:                            # uncomment to enable
    #   api_key_env: OPENAI_API_KEY
    # anthropic:
    #   api_key_env: ANTHROPIC_API_KEY

  roles:
    chat:      {provider: azure, model: gpt-5.4}         # text generation
    code:      {provider: azure, model: gpt-5.4}         # code generation
    drawing:   {provider: azure, model: gpt-image-1}     # image generation
```

**Important**: In agent mode (`plot`, `sketch`, `diagram`), agents handle their own chat/code tasks using their platform model. The `chat` and `code` roles here are only used by direct `pipeline_cli.py` subcommands. The `drawing` role is the one that matters most — it controls image generation for `diagram` mode.

> **Common mistake**: Make sure the `provider` and `model` in each role are compatible. For example, `gpt-image-1` is an OpenAI/Azure model — setting `{provider: google, model: gpt-image-1}` will fail because Google's API doesn't recognize that model. Use `{provider: azure, model: gpt-image-1}` or `{provider: google, model: gemini-2.0-flash-preview-image-generation}`.

#### LLM Presets (`--llm-preset`)

The `roles:` section above defines the **default** LLM routing that applies when you run any command without `--llm-preset`. Presets let you override specific roles at runtime without editing `pipeline.yaml`:

```bash
# Uses default roles (all Azure)
python cli.py diagram --proposal paper.md

# Override: switch all roles to Google Gemini for this run
python cli.py diagram --proposal paper.md --llm-preset gemini

# Override: keep Azure for chat/code, switch drawing to Gemini
python cli.py diagram --proposal paper.md --llm-preset mixed
```

**How it works**: A preset only overrides the roles it explicitly defines. Roles not listed in the preset keep their values from the default `roles:` section.

```yaml
# In pipeline.yaml:
presets:
  azure:
    drawing: {provider: azure, model: gpt-image-1}       # only overrides drawing
  gemini:
    chat:    {provider: google, model: gemini-2.5-flash}  # overrides all three
    code:    {provider: google, model: gemini-2.5-pro}
    drawing: {provider: google, model: gemini-2.0-flash-preview-image-generation}
  mixed:
    drawing: {provider: google, model: gemini-2.0-flash-preview-image-generation}  # only overrides drawing
```

| Preset | chat | code | drawing | Use case |
|--------|------|------|---------|----------|
| *(none)* | Azure GPT-5.4 | Azure GPT-5.4 | Azure gpt-image-1 | Default — all Azure |
| `azure` | *(default)* | *(default)* | Azure gpt-image-1 | Explicit Azure image gen |
| `gemini` | Gemini Flash | Gemini Pro | Gemini image gen | All-Google setup |
| `mixed` | *(default)* | *(default)* | Gemini image gen | Azure text + Google drawing |

To change **only one role** permanently (e.g., always use OpenAI for drawing), edit the `roles:` section in `pipeline.yaml` directly. To change it per-run, either use a preset or create your own custom preset in the `presets:` block.

### Supported Pipeline LLM Providers

These providers are used by pipeline nodes (configured in `llm:` section) — separate from the agent platform model above:

| Provider | chat | code | drawing (image gen) | Auth env var |
|----------|------|------|---------------------|-------------|
| Azure-compatible OpenAI | Yes | Yes | Yes | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` |
| Google Gemini | Yes | Yes | Yes | `GEMINI_API_KEY` or Vertex AI credentials |
| OpenAI | Yes | Yes | Yes (`gpt-image-1`) | `OPENAI_API_KEY` |
| Anthropic | Yes | Yes | No | `ANTHROPIC_API_KEY` |
| AWS Bedrock | Yes | Yes | No | `AWS_DEFAULT_REGION` + AWS credentials |

## Configuration Recipes

### Recipe 1: All-Gemini (simplest setup)

Use Gemini for both agents and pipeline LLM calls.

```bash
# .env
GEMINI_API_KEY=AIzaSy...
```

```bash
python cli.py plot --proposal paper.md --agent gemini --llm-preset gemini
```

Or make it the default in `configs/pipeline.yaml`:

```yaml
agent:
  platform: gemini
  gemini:
    model: gemini-2.5-pro

llm:
  roles:
    chat:    {provider: google, model: gemini-2.5-flash}
    code:    {provider: google, model: gemini-2.5-pro}
    drawing: {provider: google, model: gemini-2.0-flash-preview-image-generation}
```

### Recipe 2: OpenCode + Gemini Agents with OpenAI Image Generation

Use OpenCode as the agent platform with a Gemini model for reasoning, but OpenAI `gpt-image-1` for raster image generation in `diagram` mode.

```bash
# .env
GEMINI_API_KEY=AIzaSy...
OPENAI_API_KEY=sk-...
```

Edit `configs/pipeline.yaml`:

```yaml
agent:
  platform: opencode
  opencode:
    model: gemini-2.5-pro               # agents use Gemini for reasoning
    provider: gemini

llm:
  providers:
    google:
      api_key_env: GEMINI_API_KEY
    openai:                      # enable the OpenAI provider
      api_key_env: OPENAI_API_KEY

  roles:
    drawing: {provider: openai, model: gpt-image-1}   # image gen via OpenAI
```

```bash
python scripts/pipeline_cli.py services start       # required for diagram
python cli.py diagram --proposal paper.md
```

### Recipe 3: Claude Code Agents with Azure Backend

```bash
# .env
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

```bash
python cli.py plot --proposal paper.md --agent claude
```

### Recipe 4: Vertex AI (no API key, service account auth)

```bash
# .env — no API key needed
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

```bash
python cli.py diagram --proposal paper.md --agent gemini --llm-preset gemini
```

### Recipe 5: Beam Search for Best Figures

Generate multiple style and code variants, rank by critic score:

```bash
python cli.py plot --proposal paper.md --execution beam
```

Beam parameters in `configs/pipeline.yaml`:

```yaml
beam:
  width: 2            # top candidates to keep per round
  style_variants: 2   # style alternatives per experiment
  code_variants: 2    # code alternatives per style
  iterations: 2       # refinement rounds
```

### Recipe 6: Parallel Execution

Run code generation for all experiments concurrently:

```bash
python cli.py plot --proposal paper.md --execution parallel
```

## CLI Reference

```
python cli.py <command> [options]
python cli.py -h
python cli.py plot -h
```

Aliases:
- `figure` = `plot`
- `method` = `diagram`
- `method-svg` = `sketch`
- `hybrid` = `composite`

| Flag | Description | Default |
|------|-------------|---------|
| `--proposal <file>` | Path to paper proposal / description **(required)** | — |
| `--results-dir <dir>` | Directory containing experiment data | `./results` |
| `--agent <platform>` | Agent platform: `opencode`, `claude`, `codex`, `gemini` | from `pipeline.yaml` |
| `--llm-preset <name>` | LLM preset: `azure`, `gemini`, `mixed` | from `pipeline.yaml` |
| `--execution <mode>` | `sequential`: one experiment at a time; `parallel`: all experiments concurrently (max 4 threads); `beam`: style×code variant search ranked by critic score | `sequential` |
| `--verbose` | Enable verbose logging (saves prompts to run_dir) | off |

## Scoring & Quality

### Statistical figures (`plot`)

The `figure-critic` subagent scores each figure on 5 dimensions (max 10 total). The agent iterates until the score meets the threshold or hits max iterations:

```yaml
scoring:
  figure_score_threshold: 9.0    # accept if score >= this
  max_iterations: 3              # give up after this many attempts
```

### Method diagrams (`diagram`)

The advocate reviewer scores on 6 dimensions (max 12). The score must meet the document-type threshold:

```yaml
scoring:
  quality_thresholds:
    journal: 10.2       # highest bar
    conference: 9.6
    poster: 8.4
    default: 9.0
```

## Microservices (Method Diagrams)

The `diagram` command requires three microservices:

| Service | Port | Purpose |
|---------|------|---------|
| SAM3 | 8001 | Image segmentation |
| PaddleOCR | 8002 | Text detection |
| BEN2 | 8003 | Background removal |

Start/stop with:
```bash
conda activate happyfigure
python scripts/pipeline_cli.py services start
python scripts/pipeline_cli.py services stop
```

Check health with:
```bash
conda activate happyfigure
python scripts/pipeline_cli.py services health
```

`services start` launches all three services together, including PaddleOCR on port 8002. Use the `happyfigure` conda environment for these commands; OCR depends on packages installed in that environment.

The `sketch` command does **not** require microservices.

### SAM3 Model Weights

SAM3 uses a ViT-H checkpoint (~2.4 GB) hosted on HuggingFace. The model is **gated** — you must accept the license on HuggingFace before downloading.

**Step 1: Request access** — visit the SAM3 model page on HuggingFace and accept the license agreement.

**Step 2: Authenticate** — log in to HuggingFace CLI so the auto-download can access the gated model:

```bash
pip install huggingface-hub
huggingface-cli login
# Paste your HuggingFace access token (from https://huggingface.co/settings/tokens)
```

After that, the `sam3` package auto-downloads the weights on first service launch. No manual file download needed.

| Setting | Where | Default |
|---------|-------|---------|
| Auto-download | `configs/services.yaml` — leave `checkpoint_path` unset | Enabled (downloads from HuggingFace) |
| HuggingFace cache dir | `HF_HOME` env var | `~/.cache/huggingface/` |
| Local checkpoint | `configs/services.yaml` — set `checkpoint_path` | Unset |
| BPE vocab | `configs/services.yaml` — `bpe_path` | `assets/bpe_simple_vocab_16e6.txt.gz` |

**To use a local checkpoint** (e.g., on an air-gapped machine), download the weights separately and point to them in `configs/services.yaml`:

```yaml
sam3:
  checkpoint_path: /path/to/sam3_vit_h.pth
```

The path can be absolute or relative (resolved against `configs/`, repo root, then CWD). If the file is not found at the given path, the server falls back to HuggingFace auto-download.

**To pre-download weights** before first use:

```bash
# Downloads to HF_HOME (default: ~/.cache/huggingface/)
python -c "from sam3.model_builder import build_sam3_image_model; build_sam3_image_model(load_from_HF=True, device='cpu')"
```

> **Troubleshooting:** If you see `401 Unauthorized` or `Access denied` errors during SAM3 startup, you haven't accepted the model license or your HuggingFace token is missing. Run `huggingface-cli login` and verify you have access on the model page.

### BEN2 Model Weights

BEN2 uses a background-removal model from HuggingFace (~500 MB). Like SAM3, **no manual download is needed** — `from_pretrained("PramaLLC/BEN2")` auto-downloads on first launch.

| Setting | Where | Default |
|---------|-------|---------|
| Auto-download | Built into `ben2` package | Enabled (downloads `PramaLLC/BEN2` from HuggingFace) |
| HuggingFace cache dir | `HF_HOME` env var | `~/.cache/huggingface/` |
| Local model | `--model-path` CLI arg on `services.ben2.server` | Unset (auto-download) |

**To use a local model** (air-gapped machine):

```bash
# Download the model to a local directory
git lfs install
git clone https://huggingface.co/PramaLLC/BEN2 /path/to/ben2-model

# Then either set HF_HOME or pass --model-path when launching
python -m services.ben2.server --model-path /path/to/ben2-model --device cuda
```

### PaddleOCR Model Weights

PaddleOCR uses the `PaddleOCR-VL-1.5` model (~300 MB). Models are **auto-downloaded by the PaddleOCR library** on first use — no manual setup needed.

| Setting | Where | Default |
|---------|-------|---------|
| Auto-download | Built into `paddleocr`/`paddlex` packages | Enabled |
| Model name | `configs/services.yaml` — `paddleocr_model` | `PaddlePaddle/PaddleOCR-VL-1.5` |
| Cache dir | PaddlePaddle internal | `~/.paddleocr/` |

**Note:** PaddleOCR requires the `happyfigure` conda environment because `paddlepaddle-gpu==3.2.1` only has wheels for Python ≤3.11. For Python 3.12+, the CPU-only `paddlepaddle==3.2.1` is used instead.

### GPU Requirements

| Service | VRAM (approx.) | Notes |
|---------|---------------|-------|
| SAM3 | ~3 GB | ViT-H model; CPU works but is slow |
| BEN2 | ~1 GB | Background removal; CPU fallback available |
| PaddleOCR | ~1 GB | Text detection; CPU fallback available |

All three services can run on CPU (`--device cpu`) but GPU is strongly recommended. A single GPU with ≥6 GB VRAM can run all three concurrently.

**First-run timing:** Expect 2–5 minutes on first launch while models download and load. Subsequent launches use cached weights and are much faster (~30s for SAM3, ~10s for OCR/BEN2).

## Privacy Note

HappyFigure sends your research proposal text and data descriptions to third-party LLM providers (OpenAI, Google, Anthropic, etc.) for figure generation. Review your data before running the pipeline to ensure no sensitive information is included.

## Claude Code Skills

HappyFigure ships with two built-in [Claude Code skills](https://docs.anthropic.com/en/docs/claude-code/skills) (`skills/`) and also works with two global Claude Code skills (`/figures`, `/figure-planner`). Each targets a different part of the figure generation workflow.

### Available Skills

| Skill | Trigger | What it does | Services needed |
|-------|---------|-------------|-----------------|
| `/plot` (repo) | "plot my results", "make a bar chart", "visualize the data" | Launches the HappyFigure multi-agent pipeline to generate statistical figures from experiment data. Gathers proposal + results dir, picks execution strategy, runs `python cli.py plot` | No |
| `/diagram` (repo) | "draw my architecture", "method figure", "system diagram" | Launches the HappyFigure pipeline for method/architecture diagrams. Picks between `sketch` and `diagram` mode based on context, runs the appropriate command | `diagram` mode: GPU + SAM3/OCR/BEN2; `sketch` mode: No |
| `/figures` (global) | "make figures for my paper", "generate plots from my results" | Standalone skill — reads your code, data, and paper draft, then writes and executes self-contained Python plotting scripts directly. Does NOT use the HappyFigure pipeline | No |
| `/figure-planner` (global) | "plan my figures", "what plots should I need" | Planning-only skill — scans data files, reads paper text, outputs a structured `paper_summary.md` specifying exact plot types per panel. Produces a spec that `/figures` can consume | No |

### When to use `/plot` vs `/figures`

Both generate statistical figures, but they work very differently:

| | `/plot` (HappyFigure pipeline) | `/figures` (standalone skill) |
|---|---|---|
| **How it works** | Orchestrates 3 specialized agents in sequence (explore → plan → code + critic loop) via `cli.py` | Single Claude Code session writes Python scripts directly |
| **Quality control** | Automated critic subagent scores each figure on 5 dimensions; iterates until threshold (default 9.0/10) | Self-review by the same agent; manual user feedback loop |
| **Style consistency** | Uses style few-shots from `configs/statistical_examples/` and `configs/multi_panel_examples/` for in-context learning | Follows built-in publication style rules (Nature/Cell/NeurIPS conventions) |
| **Best for** | Batch generation of many figures from a proposal; reproducible pipeline runs; beam search for highest quality | Quick one-off plots; when you want to see and edit the code interactively; when data is complex and needs manual exploration |
| **Setup** | Needs HappyFigure installed + agent platform configured | Works anywhere Claude Code runs — no project setup |
| **Execution modes** | Sequential, parallel (4 concurrent), beam search (multi-variant ranking) | Sequential only |
| **Output location** | `notes/figure_runs/run_<timestamp>/` for `plot`; `notes/diagram_runs/run_<timestamp>/` for `sketch` / `diagram` / `composite` | Current directory or user-specified path |

**Rule of thumb**: Use `/plot` when you have a well-defined proposal and want automated, critic-scored figures at scale. Use `/figures` when you want hands-on control, are iterating on a single figure, or don't have HappyFigure set up.

You can also combine them: run `/figure-planner` first to get a structured spec, then feed that spec as the proposal to `/plot`.

### When to use `sketch` vs `diagram`

The `/diagram` skill chooses between these two modes, but you can also pick directly:

| | `sketch` | `diagram` |
|---|---|---|
| **Pipeline** | 2 agents: method-explore → svg-author | 3 agents: method-explore → svg-builder → svg-refiner |
| **How SVG is created** | Agent writes SVG directly from the method description text | Generates a raster image via LLM image API → segments with SAM3 → reconstructs as SVG → refines iteratively |
| **Visual fidelity** | Clean vector diagrams; style depends on agent's SVG skill and reference examples | Higher fidelity — starts from a rich raster image, preserves spatial layout and icons |
| **Review process** | Self-review loop (agent checks its own SVG, max 3 iterations) | Advocate reviewer scores on 6 dimensions (clarity, readability, hierarchy, ambiguity, publication-readiness, visual fidelity) with iterative refinement |
| **Services required** | None | SAM3 (segmentation), OCR (text detection), BEN2 (background removal) — needs GPU |
| **Runtime** | Fast (minutes) | Slower (image generation + 3 microservice passes + multi-round review) |
| **Best for** | Drafts, rapid iteration, simple pipelines/flowcharts, machines without GPU | Camera-ready figures, complex multi-component architectures, replicating existing figures as SVG |

**Rule of thumb**: Start with `sketch` for speed and iteration. Upgrade to `diagram` when you need publication-quality output with fine-grained icon detail, or when converting an existing raster figure to editable SVG.

## Troubleshooting

### Services won't start
- **SAM3 (port 8001)**: Requires a CUDA-capable GPU. Check `nvidia-smi` for available VRAM (~4 GB needed). If the port is already in use, run `python scripts/pipeline_cli.py services stop` first.
- **OCR (port 8002)**: Requires PaddlePaddle. Run inside the `happyfigure` conda environment where Paddle is installed. Not available on Python 3.13+.
- **BEN2 (port 8003)**: Requires GPU. Configure the endpoint via `BEN2_SERVICE_URL` env var or `configs/pipeline.yaml` if running remotely.

### LLM provider errors
- **`AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` not set**: Set these in your `.env` file or export them in your shell. See `.env.example` for the full list.
- **Google Gemini 403/429**: Verify `GEMINI_API_KEY` is valid. For Vertex AI, ensure `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` are set.
- **Anthropic errors**: Ensure `ANTHROPIC_API_KEY` is set. Claude Code also requires the `claude` CLI installed (`claude --version`).

### Agent platform issues
- **OpenCode not found**: Install [OpenCode](https://github.com/opencode-ai/opencode) and ensure `opencode` is on your `PATH`.
- **Codex CLI not found**: Install `@openai/codex` and ensure `codex` is on your `PATH`.
- **Agent exits immediately**: Check `notes/figure_runs/run_*/logs/` for plot runs, or `notes/diagram_runs/run_*/logs/` for method-diagram runs.

### Common runtime problems
- **`figure_code.py` fails with import errors**: Ensure `matplotlib`, `numpy`, and `pandas` are installed. Install optional extras with `pip install -e ".[examples]"`.
- **SVG validation exhausts retries**: The method description may be too complex. Simplify the proposal or increase `scoring.max_iterations` in `configs/pipeline.yaml`.
- **Low critic scores (< 7)**: Try beam search mode (`python cli.py plot --proposal ... --execution beam`) to explore more style/code variants.

## Additional Documentation

- [Onboarding Guide](docs/ONBOARD.md) — Detailed setup guide for Copilot CLI / OpenCode / Gemini CLI / Vertex AI environments

## References & Acknowledgements

HappyFigure builds on ideas, code, and models from the following projects:

### Upstream Models & Services

| Project | Used for | Contribution to HappyFigure |
|---------|----------|---------------------------|
| [SAM3](https://github.com/facebookresearch/sam3) (Facebook Research) | Image segmentation microservice (port 8001) | Segments raster method diagrams into bounding boxes for icons, shapes, and text regions — enables SVG reconstruction from generated images |
| [BEN2](https://github.com/PramaLLC/BEN2) (PramaLLC) | Background removal microservice (port 8003) | Removes backgrounds from extracted icon crops so they embed cleanly into SVGs as transparent PNGs |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) (Baidu) | Text detection microservice (port 8002) | Detects and recognizes text labels in generated raster images so they can be recreated as editable SVG `<text>` elements |

### Code & Pipeline References

| Project | What we adapted |
|---------|----------------|
| [PaperBanana](https://github.com/dwzhu-pku/PaperBanana) | Core concept of proposal-to-figure generation; experiment data exploration and planning stages |
| [AutoFigure-Edit](https://github.com/ResearAI/AutoFigure-Edit) | Raster-to-SVG conversion pipeline design; SAM-based segmentation → SVG reconstruction approach |
| [Paper2Any](https://github.com/OpenDCAI/Paper2Any) | Multi-format output pipeline architecture; LLM-driven code generation for figures |
| [Edit-Banana](https://github.com/BIT-DataLab/Edit-Banana) | Iterative critic-driven refinement loop; multi-dimension scoring rubric design |
| [figures4papers](https://github.com/ChenLiu-1996/figures4papers) | Style few-shot dataset influence; derived example assets in `configs/statistical_examples/`, `configs/multi_panel_examples/`, and related config folders are used for in-context learning |
| [claude-scientific-skills](https://github.com/K-Dense-AI/claude-scientific-skills) | Agent prompt engineering patterns for scientific figure tasks |
| [auto-figures](https://github.com/SocraLee/auto-figures) | Multi-agent orchestration pattern; sequential explore → plan → generate agent flow |
| [pubfig](https://github.com/Galaxy-Dawn/pubfig) | Publication-ready plot type catalog (41 types); journal-aware themes (Nature/Science/Lancet/JAMA); styling conventions for forest plots, volcano plots, raincloud, Sankey, UpSet, and other specialized scientific visualizations |

## License

MIT License. See [LICENSE](LICENSE) for details.
