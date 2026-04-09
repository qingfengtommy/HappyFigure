"""Auto-start and shutdown helpers for the BEN2 HTTP service.

Usage:
    from services.ben2.launcher import ensure_ben2_service, shutdown_ben2_service

    proc = ensure_ben2_service()
    # ... run pipeline ...
    shutdown_ben2_service(proc)
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

from pipeline.service_launcher import (
    ensure_service,
    resolve_device,
    resolve_hf_env,
    shutdown_service,
)

_DEFAULT_PORT = 8003
_DEFAULT_HOST = "127.0.0.1"


def ensure_ben2_service(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    device: str | None = None,
    model_path: str | None = None,
    startup_timeout: int = 120,
) -> Optional[subprocess.Popen]:
    """Ensure the BEN2 service is running. Start it if needed.

    Returns the Popen handle if we started it, or None if already running.
    """
    cmd = [
        sys.executable,
        "-m",
        "services.ben2.server",
        "--host",
        host,
        "--port",
        str(port),
        "--device",
        device or resolve_device(),
    ]
    if model_path:
        cmd.extend(["--model-path", model_path])

    return ensure_service(
        "ben2",
        cmd,
        host,
        port,
        env=resolve_hf_env(),
        startup_timeout=startup_timeout,
    )


def shutdown_ben2_service(proc: Optional[subprocess.Popen]) -> None:
    """Shut down a BEN2 service subprocess that we started."""
    shutdown_service("ben2", proc)
