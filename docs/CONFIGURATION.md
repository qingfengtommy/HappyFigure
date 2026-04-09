# Configuration Guide

All configuration lives in **`configs/pipeline.yaml`**. Override per-user without editing the original:

```bash
export HAPPYFIGURE_CONFIG=configs/pipeline_override.yaml
```

## Agent Platforms

Controls which CLI tool orchestrates agents and what model they use.

```yaml
agent:
  platform: opencode          # default platform
  opencode:
    model: gpt-5.4
    provider: azure
  claude:
    model: claude-opus-4-6
  codex:
    model: gpt-5.4
    sandbox_mode: workspace-write
    retry_dangerous_on_sandbox_failure: true
  gemini:
    model: gemini-3.1-pro-preview
  copilot:
    model: gpt-5.3-codex
```

Override at runtime: `python cli.py plot --proposal paper.md --agent gemini`

| Platform | Flag | CLI tool needed | Status |
|----------|------|-----------------|--------|
| OpenCode | `--agent opencode` | [`opencode`](https://github.com/sst/opencode) | Stable (default) |
| Claude Code | `--agent claude` | [`claude`](https://docs.anthropic.com/en/docs/claude-code) | Stable |
| Codex | `--agent codex` | [`codex`](https://github.com/openai/codex) | Experimental |
| Gemini CLI | `--agent gemini` | [`gemini`](https://github.com/google-gemini/gemini-cli) | Experimental |
| Copilot | `--agent copilot` | GitHub Copilot CLI | Experimental (may not work with current Copilot CLI) |

The orchestrator reads `agent.<platform>.model` and `agent.<platform>.provider`, then generates agent config files with that model for the selected platform. Agent prompt templates live in `prompts/agents/`.

For Codex, `sandbox_mode` controls the normal launch mode. If Codex exits with a known Linux bubblewrap startup error, `retry_dangerous_on_sandbox_failure: true` retries once with `danger-full-access`. This is useful in restricted container or AppArmor environments but trades away Codex's sandbox.

### OpenCode Provider Auto-Discovery

OpenCode has [75+ built-in providers](https://opencode.ai/docs/providers/) (GitHub Copilot, Google, Anthropic, OpenAI, and many more). For these, **no configuration is needed in `opencode.jsonc`** — models are auto-discovered once you authenticate:

```bash
# One-time setup: authenticate with a built-in provider
opencode          # then run /connect and select your provider (e.g., GitHub Copilot)

# See all available models across connected providers
opencode          # then run /models
```

After connecting, use any model from that provider in `pipeline.yaml`:

```yaml
agent:
  opencode:
    model: gemini-3.1-pro-preview             # model ID from the provider
    provider: github-copilot           # built-in provider name
```

The model string `github-copilot/gemini-3.1-pro-preview` is written into each agent's frontmatter and OpenCode resolves it automatically.

**When do you need a custom provider in `opencode.jsonc`?** Only for non-standard endpoints — for example, a custom Azure OpenAI deployment:

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

---

## LLM Provider Routing

Controls which API is called for each pipeline role:

```yaml
llm:
  providers:
    azure:
      api_key_env: AZURE_OPENAI_API_KEY   # also set AZURE_OPENAI_ENDPOINT
    google:
      api_key_env: GEMINI_API_KEY
    # openai:
    #   api_key_env: OPENAI_API_KEY
    # anthropic:
    #   api_key_env: ANTHROPIC_API_KEY

  roles:
    chat:      {provider: azure, model: gpt-5.4}         # text generation
    code:      {provider: azure, model: gpt-5.4}         # code generation
    drawing:   {provider: azure, model: gpt-image-1}     # image generation
```

**Important**: In agent mode (`plot`, `sketch`, `diagram`), agents handle their own chat/code tasks using their platform model. The `chat` and `code` roles are only used by direct `pipeline_cli.py` subcommands. The `drawing` role matters most — it controls image generation for `diagram` mode.

> **Common mistake**: Make sure the `provider` and `model` in each role are compatible. For example, `gpt-image-1` is an OpenAI/Azure model — setting `{provider: google, model: gpt-image-1}` will fail. Use `{provider: azure, model: gpt-image-1}` or `{provider: google, model: gemini-3.1-flash-image-preview}`.

### LLM Presets (`--llm-preset`)

Presets override specific roles at runtime without editing `pipeline.yaml`:

```bash
python cli.py diagram --proposal paper.md                      # uses default roles (all Azure)
python cli.py diagram --proposal paper.md --llm-preset gemini  # switch all roles to Gemini
python cli.py diagram --proposal paper.md --llm-preset mixed   # Azure text + Gemini drawing
```

A preset only overrides the roles it explicitly defines. Others keep their default values.

```yaml
presets:
  azure:
    drawing: {provider: azure, model: gpt-image-1}
  gemini:
    chat:    {provider: google, model: gemini-3.1-pro-preview}
    code:    {provider: google, model: gemini-3.1-pro-preview}
    drawing: {provider: google, model: gemini-3.1-flash-image-preview}
  mixed:
    drawing: {provider: google, model: gemini-3.1-flash-image-preview}
```

| Preset | chat | code | drawing | Use case |
|--------|------|------|---------|----------|
| *(none)* | Azure GPT-5.4 | Azure GPT-5.4 | Azure gpt-image-1 | Default — all Azure |
| `azure` | *(default)* | *(default)* | Azure gpt-image-1 | Explicit Azure image gen |
| `gemini` | Gemini Pro | Gemini Pro | Gemini image gen | All-Google setup |
| `mixed` | *(default)* | *(default)* | Gemini image gen | Azure text + Google drawing |

### Supported Providers

| Provider | chat | code | drawing | Auth env var |
|----------|------|------|---------|-------------|
| Azure-compatible OpenAI | Yes | Yes | Yes | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` |
| Google Gemini | Yes | Yes | Yes | `GEMINI_API_KEY` or Vertex AI credentials |
| OpenAI | Yes | Yes | Yes (`gpt-image-1`) | `OPENAI_API_KEY` |
| Anthropic | Yes | Yes | No | `ANTHROPIC_API_KEY` |
| AWS Bedrock | Yes | Yes | No | `AWS_DEFAULT_REGION` + AWS credentials |

---

## Configuration Recipes

### Recipe 1: All-Gemini (simplest setup)

```bash
# .env
GEMINI_API_KEY=AIzaSy...
```

```bash
python cli.py plot --proposal paper.md --agent gemini --llm-preset gemini
```

Or make it the default:

```yaml
agent:
  platform: gemini
  gemini:
    model: gemini-3.1-pro-preview

llm:
  roles:
    chat:    {provider: google, model: gemini-3.1-pro-preview}
    code:    {provider: google, model: gemini-3.1-pro-preview}
    drawing: {provider: google, model: gemini-3.1-flash-image-preview}
```

### Recipe 2: OpenCode + Gemini Agents with OpenAI Image Generation

```bash
# .env
GEMINI_API_KEY=AIzaSy...
OPENAI_API_KEY=sk-...
```

```yaml
agent:
  platform: opencode
  opencode:
    model: gemini-3.1-pro-preview
    provider: gemini

llm:
  providers:
    google:
      api_key_env: GEMINI_API_KEY
    openai:
      api_key_env: OPENAI_API_KEY
  roles:
    drawing: {provider: openai, model: gpt-image-1}
```

```bash
python scripts/pipeline_cli.py services start
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

### Recipe 4: Vertex AI (service account auth, no API key)

```bash
# .env
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

```bash
python cli.py diagram --proposal paper.md --agent gemini --llm-preset gemini
```

### Recipe 5: Beam Search for Best Figures

```bash
python cli.py plot --proposal paper.md --execution beam
```

```yaml
beam:
  width: 2            # top candidates to keep per round
  style_variants: 2   # style alternatives per experiment
  code_variants: 2    # code alternatives per style
  iterations: 2       # refinement rounds
```

### Recipe 6: Parallel Execution

```bash
python cli.py plot --proposal paper.md --execution parallel
```
