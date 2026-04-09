# HappyFigure LLM Routing

## 1. Why Are There So Many Model References?

Models appear in **4 places**. Here's what each one does:

| # | File | What it configures | Who reads it | Edit it? |
|---|---|---|---|---|
| 1 | `configs/pipeline.yaml` → `llm.roles` | Pipeline node LLM calls (`llm.run_prompt("chat"/"code"/"drawing")`) | Python code in `graphs/*.py` | Only `drawing` matters in agent mode |
| 2 | `configs/pipeline.yaml` → `agent.opencode` | Template model for all agents | `agents/opencode.py` at startup | Override via `HAPPYFIGURE_CONFIG` |
| 3 | `.opencode/opencode.jsonc` → `"model"` | OpenCode's default model + provider registry | `opencode` binary | Edit directly |
| 4 | `.opencode/agent/*.md` → `model:` frontmatter | Per-agent model assignment | `opencode` binary | Auto-generated from #2 |

### In agent mode (current setup):
- **#1 `llm.roles`** — `chat` and `code` roles are unused by agents. Only `drawing` matters (for `diagram` mode image generation).
- **#2 `agent.opencode`** — The orchestrator reads `provider` + `model` from here and writes them into every agent `.md` file.
- **#3 `opencode.jsonc`** — Must list the provider and set the correct default model.
- **#4 Agent `.md` files** — Regenerated every time `cli.py` starts. Never edit manually.

### The `opencode.jsonc` provider block

This is OpenCode's provider registry. It tells OpenCode how to connect to model APIs:
- `azure` — Azure OpenAI endpoint
- `gemini` — Google Gemini models via API key
- `github-copilot` — built-in, no config needed (uses OAuth from `opencode providers`)

You only need providers you actually use.

---

## 2. Which Agents Run for Each Command?

### `plot` — Statistical figures (most common)

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | `data-explore` | Explores data files, writes exploration report + summary JSON |
| 2 | `planner-stylist` | Plans figures, writes styled specs per experiment |
| 3 | `code-agent` | Writes Python matplotlib code, executes it |
| 3a | `figure-critic` (subagent) | Scores the generated figure, provides feedback |

**All 4 agents use ONE model** (e.g., `github-copilot/gpt-5.4`).
No image generation API calls. No Vertex AI.

### `sketch` — Architecture diagram (lightweight, no services)

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | `method-explore` | Reads proposal, writes method description |
| 2 | `svg-author` | Writes SVG code directly as architecture diagram |

**2 agents**, same model. Agent writes SVG as code — no image API.

### `diagram` — Architecture diagram (full pipeline, needs services)

| Step | Agent | What it does |
|------|-------|-------------|
| 1 | `method-explore` | Reads proposal, writes method description |
| 2 | `svg-builder` | Generates raster image, runs SAM3 segmentation, writes SVG |
| 3 | `svg-refiner` | Iteratively improves SVG by comparing with source image |

**3 agents** + the `drawing` role (image generation via Vertex AI).
This is the **only mode** that uses Vertex AI.

### Summary

| Command | Agents used | Uses Vertex AI? | Uses agent platform? |
|---------|------------|-----------------|----------------------|
| `plot` | data-explore, planner-stylist, code-agent, figure-critic | No | Yes (all agents) |
| `sketch` | method-explore, svg-author | No | Yes (all agents) |
| `diagram` | method-explore, svg-builder, svg-refiner | Yes (drawing only) | Yes (all agents) |

---

## 3. Example Setup: GitHub Copilot + Vertex AI

### How it works

```
 ┌─────────────────────────────────────────────────┐
 │  OpenCode agents (all commands)                  │
 │  model: github-copilot/gpt-5.4                  │
 │  auth: GitHub OAuth (built-in)                   │
 │                                                  │
 │  Does: explore data, plan figures, write code,   │
 │        score figures, write SVG                  │
 └──────────────────────┬──────────────────────────┘
                        │
     (only for `diagram` command)
                        ▼
 ┌─────────────────────────────────────────────────┐
 │  Google Vertex AI (drawing role only)            │
 │  model: gemini-3.1-flash-image-preview           │
 │  auth: GOOGLE_APPLICATION_CREDENTIALS            │
 │                                                  │
 │  Does: generate raster image for SVG conversion  │
 └─────────────────────────────────────────────────┘
```

### Config files

**`.opencode/opencode.jsonc`**:
```jsonc
"model": "github-copilot/gpt-5.4",
```

**`configs/pipeline_override.yaml`** (activate with `export HAPPYFIGURE_CONFIG=configs/pipeline_override.yaml`):
```yaml
agent:
  opencode:
    model: gpt-5.4
    provider: github-copilot
llm:
  presets:
    gemini:
      drawing: {provider: google, model: gemini-3.1-flash-image-preview}
```

**`.env`**:
```bash
GOOGLE_CLOUD_PROJECT=<your-gcp-project-id>
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
GEMINI_MODEL_DRAWING=gemini-3.1-flash-image-preview
```

### Available Copilot models (via OpenCode)

```
github-copilot/gpt-5.4              <- current default
github-copilot/gpt-5.3-codex        <- code-optimized
github-copilot/claude-opus-4.6
github-copilot/claude-sonnet-4.6
github-copilot/gemini-3.1-pro-preview
github-copilot/gpt-5.4-mini         <- faster/cheaper
```

All agents use the same model right now. Per-agent models would need changes to `agents/opencode.py`.

---

## 4. Multi-User Config Strategy

### Problem

Multiple users with different setups need to work from the same repo without conflicts.

### Recommended file layout

```
configs/
  pipeline.yaml                    # checked in — sensible defaults
  pipeline_override.yaml           # gitignored — user's actual config
.opencode/
  opencode.jsonc                   # checked in — provider registry
  agent/                           # gitignored — auto-generated per run
prompts/
  agents/                          # checked in — shared prompt templates (source of truth)
.env                               # gitignored — user's API keys
```

### How to customize

1. Copy `.env.example` to `.env` and set your API keys
2. Create `configs/pipeline_override.yaml` with your overrides (see example above)
3. Export: `export HAPPYFIGURE_CONFIG=configs/pipeline_override.yaml`
4. Agent `.md` files are auto-generated — never edit them manually

### Environment variable overrides

| Variable | Purpose |
|----------|---------|
| `HAPPYFIGURE_CONFIG` | Path to config overlay YAML |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI base URL |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GOOGLE_CLOUD_PROJECT` | GCP project for Vertex AI |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to GCP service account JSON |
