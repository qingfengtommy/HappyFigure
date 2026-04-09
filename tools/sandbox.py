"""Path sandboxing utility.

Every tool that touches the filesystem must call ``safe_resolve`` before
opening any file.  This prevents path-traversal attacks where an LLM
supplies ``../../etc/passwd`` or an absolute path outside the sandbox.
"""

from __future__ import annotations

from pathlib import Path


def safe_resolve(file_path: str, base_dir: Path) -> Path | None:
    """Resolve *file_path* relative to *base_dir* and verify containment.

    Returns the resolved ``Path`` if it lives inside *base_dir*, otherwise
    ``None`` (traversal detected or absolute path outside sandbox).
    """
    # Reject null bytes (could bypass checks on some OSes)
    if "\x00" in file_path:
        return None

    resolved = (base_dir / file_path).resolve()
    if not resolved.is_relative_to(base_dir.resolve()):
        return None
    return resolved
