"""HappyFigure pipeline package.

Submodules:
    context         — RunnerContext replacing the global _orchestrator
    run_state       — state.json I/O and directory layout helpers
    agent_runtime   — agent subprocess execution (pipe, PTY, retry)
    pipeline_backend— subprocess calls to pipeline_cli.py and services
    plot_planning   — planner-stylist prompt building and spec validation
    plot_execution  — sequential/parallel code-agent execution
    plot_beam       — beam search (style variants, ranking, refinement)
    drawing         — SVG/diagram/sketch/composite agent steps
"""
