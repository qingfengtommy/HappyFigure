# HappyFigure Cheatsheet

## Setup (run once per terminal session)

```bash
cd /path/to/HappyFigure
conda activate happyfigure
export $(grep -v '^#' .env | xargs)
```

Use the `happyfigure` conda environment for all HappyFigure commands. This is especially required for the OCR service because its Paddle dependencies live in that environment.

## Commands

### Statistical plots
```bash
conda activate happyfigure
python cli.py plot --proposal configs/example_proposal.txt
```

### Architecture diagram — lightweight (no services needed)
```bash
conda activate happyfigure
python cli.py sketch --proposal configs/example_proposal.txt
```

### Architecture diagram — full pipeline (needs services)
```bash
conda activate happyfigure

# Start services first
python scripts/pipeline_cli.py services start

# Verify SAM3 / OCR / BEN2 are all healthy
python scripts/pipeline_cli.py services health

# Run diagram
python cli.py diagram --proposal configs/example_proposal.txt --llm-preset gemini

# Stop services when done
python scripts/pipeline_cli.py services stop
```

### Architecture diagram with existing image
```bash
conda activate happyfigure
python cli.py diagram --proposal paper.md --drawing-image /path/to/image.png --llm-preset gemini
```

## Services

```bash
conda activate happyfigure

# Start all (SAM3:8001, OCR:8002, BEN2:8003)
python scripts/pipeline_cli.py services start

# OCR is started by the same command above; no separate OCR start command is needed
# If OCR is unhealthy, re-check that you are in the happyfigure conda env

# Check health
python scripts/pipeline_cli.py services health

# Stop all
python scripts/pipeline_cli.py services stop

# Quick health check via curl
curl -s http://127.0.0.1:8001/health  # SAM3
curl -s http://127.0.0.1:8002/health  # OCR
curl -s http://127.0.0.1:8003/health  # BEN2
```

## LLM presets
```bash
--llm-preset gemini        # All roles use Google Gemini
--llm-preset azure         # All roles use Azure OpenAI
--llm-preset mixed         # Azure text + Google drawing
```

## Agent platform switching
```bash
# Default: OpenCode
python cli.py plot --proposal paper.md --llm-preset gemini

# Switch to Codex — add --agent codex
python cli.py --agent codex plot --proposal paper.md --llm-preset gemini

# Permanent switch: set in pipeline_override.yaml
# agent:
#   platform: codex
```

## Agent model (set via pipeline_override.yaml)
```yaml
# configs/pipeline_override.yaml
agent:
  # platform: codex              # uncomment for permanent switch
  opencode:
    model: gpt-5.4
    provider: azure
  codex:
    model: gpt-5.4
    sandbox_mode: danger-full-access
```

## OpenCode (direct agent testing)
```bash
# Test an agent directly
opencode run --agent data-explore "Say hello"
opencode models
opencode providers list
```

## Codex (direct agent testing)
```bash
# Quick test
codex -a never exec --skip-git-repo-check -m gpt-5.4 -s danger-full-access "echo hello"
```

## Git (ignore auto-generated agent files)
```bash
# Stop tracking local changes to agent .md files
git update-index --assume-unchanged .opencode/agent/*.md

# Undo
git update-index --no-assume-unchanged .opencode/agent/*.md
```

## Vertex AI drawing test
```bash
python3 -c "
from dotenv import load_dotenv; load_dotenv()
from llm.gemini_example import _get_client, run_prompt
client = _get_client()
print('Client:', type(client).__name__)
result = run_prompt('chat', 'Say hello')
print('Response:', result[:50])
"
```

## Useful paths
```
configs/pipeline.yaml              # Main pipeline config (don't edit for personal use)
configs/pipeline_override.yaml     # Your personal overrides (HAPPYFIGURE_CONFIG)
.opencode/opencode.jsonc           # OpenCode provider + default model config
.opencode/agent/*.md               # Auto-generated agent files (don't commit)
.env                               # API keys, env vars (gitignored)
notes/diagram_runs/                # diagram/sketch output runs
notes/figure_runs/                 # plot output runs
configs/method_examples/           # Reference architecture diagrams
configs/statistical_examples/      # Reference statistical figures
```
