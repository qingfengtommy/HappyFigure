# Changelog

All notable changes to HappyFigure will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Refactored `_step_execute_beam` in `cli.py` into smaller helper functions
- Refactored `sam3_merge_classify_node` in `svg_method_pipeline.py` — extracted LLM classification into helper
- Added `__all__` exports to `llm`, `orchestrator`, and `prompts` packages
- Improved agent description clarity for `viz-composer` and `stat-figure`
- Expanded documentation (CONTRIBUTING, SECURITY, troubleshooting FAQ)
- Improved CI workflow with test result uploads and pip caching

### Removed
- Deprecated `svg-diagram` agent (superseded by `svg-builder` + `svg-refiner`)
- Removed `figure_retrieve/` module (unused FAISS-based retrieval)

## [0.1.0] - 2026-04-02

### Added
- Core pipeline commands: `plot`, `diagram`, `sketch`, `composite`
- Multi-provider LLM routing via `configs/pipeline.yaml` (Azure, Google Gemini, OpenAI, Anthropic, AWS Bedrock)
- Agent orchestration with platform adapters (OpenCode, Claude Code, Codex, Gemini CLI)
- SVG method drawing pipeline (21 nodes: image gen, SAM3 segmentation, SVG generation, review loop)
- Statistical figure generation with critic scoring and iterative refinement
- Beam search execution mode for style/code variant exploration
- Microservices for image processing (SAM3, PaddleOCR, BEN2)
- Composite mode: diagram + programmatic visualization compositing (4-agent pipeline)
- Sketch mode: lightweight agent-driven SVG generation without microservices
- CLI presets (`--llm-preset`) for quick LLM configuration switching
- Rich terminal UI with progress tracking
- Parallel execution mode for multi-experiment figure generation
- Style few-shot examples from figures4papers dataset
- Comprehensive documentation (README, CONTRIBUTING, SECURITY, onboarding guide)
- Pre-commit hooks with ruff linting and formatting
- GitHub Actions CI for lint + test on Python 3.10–3.12

[Unreleased]: https://github.com/qingfengtommy/HappyFigure/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/qingfengtommy/HappyFigure/releases/tag/v0.1.0
