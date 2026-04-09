"""Shared pipeline helper functions."""
from __future__ import annotations


def build_experiments_context_markdown(experiments_dir: str, max_files: int = 120) -> str:
    """Build a compact markdown summary for experiments_dir."""
    from pathlib import Path

    root = Path(experiments_dir)
    if not root.exists():
        return f"# Experiments context\n\nPath does not exist: {experiments_dir}\n"
    if not root.is_dir():
        return f"# Experiments context\n\nPath is not a directory: {experiments_dir}\n"

    data_exts = {".tsv", ".csv", ".json", ".md", ".npy", ".npz", ".pkl", ".pt", ".ckpt"}
    files = sorted([p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in data_exts], key=lambda p: str(p))
    shown = files[:max_files]
    lines = [
        "# Experiments context",
        "",
        f"Root: {root}",
        f"Data files found: {len(files)}",
        "",
        "## File list",
    ]
    for p in shown:
        rel = p.relative_to(root)
        lines.append(f"- `{rel}`")
    if len(files) > len(shown):
        lines.append(f"- ... ({len(files) - len(shown)} more)")
    return "\n".join(lines) + "\n"
