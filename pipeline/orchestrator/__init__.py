"""Agent-first pipeline orchestration (stages, artifact layout v2).

Import concrete entrypoints from ``pipeline.orchestrator.main`` to avoid import cycles
(``main`` pulls in ``plot_planning`` / ``plot_beam``, which import ``artifacts``).
"""
