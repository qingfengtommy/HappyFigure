# HappyFigure Onboarding — Your Setup

> Generated for: OpenCode + Gemini CLI + Vertex AI environment

## TL;DR

| What you have | What HappyFigure supports | Match? |
|---|---|---|
| **OpenCode** | ✅ Default agent platform | ✅ Ready |
| **Codex** | ✅ Full adapter with sandbox retry | ✅ Ready |
| **Gemini CLI** (`gemini`) | ✅ Experimental adapter | ✅ Ready |
| **Vertex AI** (service account JSON) | ✅ Built-in | ✅ Working (location=global) |

**Recommended path**: Use **OpenCode** (default) or **Codex** (`--agent codex`) as agent platform, with `--llm-preset gemini` for image generation via Google Vertex AI.

---

## 1. Architecture Overview

HappyFigure has two separate LLM integration layers:

```
┌─────────────────────────────────────────────────────┐
│                   cli.py                        │
│                  (orchestrator)                       │
├────────────────────┬────────────────────────────────┤
│  Agent Platform    │  LLM Provider (pipeline nodes) │
│  ───────────────   │  ────────────────────────────── │
│  Drives multi-step │  Direct API calls for:         │
│  tasks via CLI:    │   • chat (text gen)             │
│   • opencode       │   • code (figure code gen)      │
│   • gemini         │   • drawing (image gen)          │
│   • claude code    │                                 │
│   • codex          │  Providers:                     │
│                    │   • Google Gemini ← you want    │
│                    │   • Azure OpenAI                │
│                    │   • OpenAI                      │
│                    │   • Anthropic                   │
│                    │   • AWS Bedrock                 │
└────────────────────┴────────────────────────────────┘
```

- **Agent platform** = which CLI tool runs multi-step reasoning (explores data, writes code, critiques results)
- **LLM provider** = which API the pipeline nodes call directly for text/code/image generation

These are **independent**: you can use OpenCode as agent platform + Gemini as LLM provider.

## 2. Three Pipeline Modes

| Command | What it does | Services needed? |
|---|---|---|
| `python cli.py plot` or `python cli.py figure` | Statistical figures from experiment data | No |
| `python cli.py sketch` or `python cli.py method-svg` | Method/architecture diagram (SVG only) | No |
| `python cli.py diagram` or `python cli.py method` | Full method diagram (image gen → SAM3 → SVG → review) | Yes (SAM3, OCR, BEN2 on ports 8001-8003) |
| `python cli.py composite` or `python cli.py hybrid` | Full method diagram plus visualization compositing | Yes (SAM3, OCR, BEN2 on ports 8001-8003 for the diagram stage) |

Start with `plot` or `sketch` — no microservices needed.

## 3. Vertex AI Auth (Already Built-in)

`llm/gemini_example.py` supports both API key and Vertex AI auth out of the box.
If `GEMINI_API_KEY` is set, it uses API key auth. Otherwise, it falls back to
Vertex AI using `GOOGLE_CLOUD_PROJECT` + `GOOGLE_APPLICATION_CREDENTIALS`.

No patches or code changes needed.

## 4. Step-by-Step Setup

### Step 1: Create HappyFigure `.env`

```bash
cd /path/to/HappyFigure

cat > .env << 'EOF'
# Vertex AI auth (uses GOOGLE_APPLICATION_CREDENTIALS / ADC)
GOOGLE_CLOUD_PROJECT=<your-gcp-project-id>
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=$HOME/.gemini/<your-service-account>.json

# Optional Gemini backend defaults — align these with the shipped gemini preset if desired
GEMINI_MODEL_CHAT=gemini-3.1-pro-preview
GEMINI_MODEL_CODE=gemini-3.1-pro-preview
GEMINI_MODEL_DRAWING=gemini-3.1-flash-image-preview

# Uncomment if you get an API key instead:
# GEMINI_API_KEY=AIzaSy...
EOF
```

> **Note**: Both the low-level Gemini backend in `llm/gemini_example.py` and the `--llm-preset gemini` in `configs/pipeline.yaml` use the same defaults: `gemini-3.1-pro-preview` (chat/code) and `gemini-3.1-flash-image-preview` (drawing). Override with `GEMINI_MODEL_*` env vars if needed.

### Step 2: Install HappyFigure (editable)

```bash
cd /path/to/HappyFigure
pip install -e ".[google]"
```

### Step 3: Configure agent platform

**Option A — OpenCode (recommended)**:

OpenCode is the default and most stable platform. The `.opencode/` directory is already configured. You need to configure OpenCode's own model provider.

The checked-in `opencode.jsonc` is configured for Azure OpenAI. To use Gemini instead, edit `.opencode/opencode.jsonc`:
```jsonc
{
  "model": "google/gemini-3.1-pro-preview",
  // ... keep existing permission config
}
```

OpenCode's Gemini provider (`@ai-sdk/google`) uses API key auth. Set `GEMINI_API_KEY` for OpenCode, and use Vertex AI for pipeline LLM calls.

**Option B — Gemini CLI**:

```bash
python cli.py plot --proposal configs/example_proposal.txt \
  --llm-preset gemini --agent gemini
```

Gemini CLI uses its own auth (your `~/.gemini/settings.json` with Vertex AI mode). This is already configured.

### Step 4: Run a test

```bash
# Simplest test — statistical plot with Gemini LLM preset
python cli.py plot \
  --proposal configs/example_proposal.txt \
  --llm-preset gemini \
  --agent gemini

# Or with OpenCode
python cli.py plot \
  --proposal configs/example_proposal.txt \
  --llm-preset gemini \
  --agent opencode
```

`plot` output goes to `runs/figure_runs/run_YYYYMMDD_HHMMSS/`.
Method runs (`sketch`, `diagram`, `composite`) go to `runs/diagram_runs/run_YYYYMMDD_HHMMSS/`.
Paper runs (`paper`) go to `runs/paper_runs/run_YYYYMMDD_HHMMSS/`.

## 5. Agent Platform Comparison

| Feature | OpenCode | Codex | Gemini CLI |
|---|---|---|---|
| **Stability** | ✅ Stable (default) | ✅ Stable | ⚠️ Experimental |
| **Installed** | ✅ v1.3.2 | ✅ v0.117.0 | ✅ Available |
| **Model** | `gpt-5.4` (default) | `gpt-5.4` | `gemini-3.1-pro-preview` |
| **Agent files** | `.opencode/agent/*.md` | `AGENTS.md` (project root) | `GEMINI.md` per-agent |
| **Auth** | Azure OpenAI / built-in provider auth | `CODEX_API_KEY` / Azure OpenAI | Vertex AI / API key |
| **How it works** | `opencode run --agent <name> <prompt>` | `codex exec -m <model> <prompt>` | `gemini -m <model> -p <prompt>` |
| **Sandbox** | None | `workspace-write` by default; retries with `danger-full-access` on sandbox bootstrap failure | Built-in |
| **Permissions** | Fine-grained per-tool | Write-level | Built-in shell/file/web |

**Switching agent platform:**
```bash
# Per-run (recommended — keeps both options available)
python cli.py --agent codex plot --proposal paper.md --llm-preset gemini

# Permanent: set agent.platform in pipeline_override.yaml
agent:
  platform: codex
```

## 6. Pipeline Config (`configs/pipeline.yaml`)

Current defaults target Azure. For Gemini, use `--llm-preset gemini` which overrides:

```yaml
# What --llm-preset gemini sets:
chat:     {provider: google, model: gemini-3.1-pro-preview}
code:     {provider: google, model: gemini-3.1-pro-preview}
drawing:  {provider: google, model: gemini-3.1-flash-image-preview}
```

You can also edit `pipeline.yaml` to make Gemini the default.

## 7. Services (Optional — only for `diagram` command)

| Service | Port | Purpose | Needed for plot/sketch? |
|---|---|---|---|
| SAM3 | 8001 | Image segmentation | No |
| PaddleOCR | 8002 | Text detection | No |
| BEN2 | 8003 | Background removal | No |

Skip these initially. If you need `diagram` mode later, first activate the `happyfigure` conda environment. This is required for the OCR service because PaddleOCR and PaddlePaddle are installed there.

```bash
conda activate happyfigure
python scripts/pipeline_cli.py services start
python scripts/pipeline_cli.py services health
```

`services start` launches all three services together, including OCR on port 8002. There is no separate OCR start command.

Note: PaddleOCR requires `paddlepaddle-gpu` which has no Python 3.13 wheel. You'd need Python 3.12 or skip OCR.

## 8. Key Files Reference

| File | Purpose |
|---|---|
| `cli.py` | Main entry point — CLI orchestrator |
| `configs/pipeline.yaml` | LLM provider routing, scoring thresholds, beam config |
| `configs/services.yaml` | Microservice endpoints and SAM3 config |
| `.opencode/opencode.jsonc` | OpenCode provider & model config |
| `.opencode/agent/*.md` | OpenCode agent definitions (pre-generated) |
| `llm/__init__.py` | Config-driven LLM router |
| `llm/gemini_example.py` | Gemini API backend (supports API key + Vertex AI) |
| `llm/providers/google_provider.py` | Google provider wrapper |
| `agents/opencode.py` | OpenCode platform adapter |
| `agents/gemini.py` | Gemini CLI platform adapter |
| `prompts/agents/*.md` | Shared agent prompt templates |
| `.env` / `.env.example` | API keys and environment config |

## 9. Quickstart Summary

```bash
# 1. Create .env
cd /path/to/HappyFigure
cat > .env << 'EOF'
GOOGLE_CLOUD_PROJECT=<your-gcp-project-id>
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=$HOME/.gemini/<your-service-account>.json
GEMINI_MODEL_CHAT=gemini-3.1-pro-preview
GEMINI_MODEL_CODE=gemini-3.1-pro-preview
GEMINI_MODEL_DRAWING=gemini-3.1-flash-image-preview
EOF

# 2. Install
conda activate happyfigure
pip install -e ".[google]"

# 3. Run (using Gemini CLI as agent + Gemini as LLM)
conda activate happyfigure
python cli.py plot \
  --proposal configs/example_proposal.txt \
  --llm-preset gemini \
  --agent gemini
```
