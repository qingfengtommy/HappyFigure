"""CLI command → internal pipeline mode."""

from __future__ import annotations

import argparse


def resolve_mode(args: argparse.Namespace) -> str:
    command = getattr(args, "command", None)
    if command in ("plot", "figure"):
        return "exp_plot"
    if command in ("diagram", "method"):
        return "composite"
    if command in ("sketch", "method-svg"):
        return "agent_svg"
    if command in ("composite", "hybrid"):
        return "composite"
    if command in ("paper", "paper-composite"):
        return "paper_composite"
    return "happyfigure"
