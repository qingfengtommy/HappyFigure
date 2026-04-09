"""Critic review tool — structured figure quality assessment.

Provides a ``submit_review`` tool for agent-driven critic scoring with
validated dimension scores. The CLI path (``scripts/figure_critic_cli.py``)
still uses regex parsing for direct LLM calls without tool-calling.
"""

from __future__ import annotations

# Dimension keys and their allowed (min, max) ranges
_DIMENSION_RANGES = {
    "data_accuracy": (0, 2),
    "clarity": (0, 2),
    "accessibility": (0, 2),
    "layout": (0, 2),
    "publication_readiness": (0, 2),
    "confusion_penalty": (-2, 0),
}


def _validate_review(review: dict) -> dict | None:
    """Return an error dict if *review* is malformed, else ``None``."""

    # Required top-level keys
    for key in ("score", "verdict", "dimension_scores", "strengths", "issues"):
        if key not in review:
            return {"error": f"Missing required field: {key}"}

    # Score range
    score = review["score"]
    if not isinstance(score, (int, float)) or score < 0 or score > 10:
        return {"error": f"Score must be 0-10, got {score}"}

    # Verdict
    if review["verdict"] not in ("ACCEPT", "NEEDS_IMPROVEMENT"):
        return {"error": f"Verdict must be ACCEPT or NEEDS_IMPROVEMENT, got {review['verdict']}"}

    # Dimension scores
    dims = review.get("dimension_scores", {})
    for dim, (lo, hi) in _DIMENSION_RANGES.items():
        if dim not in dims:
            return {"error": f"Missing dimension score: {dim}"}
        val = dims[dim]
        if not isinstance(val, (int, float)) or val < lo or val > hi:
            return {"error": f"Dimension {dim} must be {lo}-{hi}, got {val}"}

    # Validate that score ≈ sum of dimensions (allow ±0.1 tolerance for rounding)
    expected = sum(dims[d] for d in _DIMENSION_RANGES)
    if abs(score - expected) > 0.1:
        return {"error": f"Score {score} != sum of dimensions {expected:.1f}"}

    # Strengths and issues must be lists
    if not isinstance(review.get("strengths"), list):
        return {"error": "strengths must be a list"}
    if not isinstance(review.get("issues"), list):
        return {"error": "issues must be a list"}

    # Validate issue item structure
    for i, issue in enumerate(review["issues"]):
        if not isinstance(issue, dict):
            return {"error": f"issues[{i}] must be a dict, got {type(issue).__name__}"}
        if "description" not in issue:
            return {"error": f"issues[{i}] missing required 'description' field"}

    return None  # valid


def execute_critic_tool(tool_name: str, args) -> dict:
    """Dispatch a critic tool call.  Currently only ``submit_review``."""
    if tool_name != "submit_review":
        return {"error": f"Unknown critic tool: {tool_name}"}

    if not isinstance(args, dict):
        return {"error": f"Expected dict arguments, got {type(args).__name__}"}

    error = _validate_review(args)
    if error is not None:
        return error

    # Return the validated review back (the pipeline node extracts fields)
    return {
        "status": "accepted",
        "score": args["score"],
        "verdict": args["verdict"],
        "dimension_scores": args["dimension_scores"],
        "strengths": args["strengths"],
        "issues": args["issues"],
    }
