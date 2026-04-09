"""Auto-start and shutdown helpers for the SAM3 HTTP service.

Usage:
    from services.sam3.launcher import ensure_sam3_service, shutdown_sam3_service

    proc = ensure_sam3_service(config_path="configs/services.yaml")
    # ... run pipeline ...
    shutdown_sam3_service(proc)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from pipeline.service_launcher import (
    ensure_service,
    resolve_device,
    resolve_hf_env,
    shutdown_service,
)

_DEFAULT_PORT = 8001
_DEFAULT_HOST = "127.0.0.1"


def ensure_sam3_service(
    config_path: str = "configs/services.yaml",
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    device: str | None = None,
    startup_timeout: int = 120,
) -> Optional[subprocess.Popen]:
    """Ensure the SAM3 service is running. Start it if needed.

    Returns:
        The Popen handle if we started the service (caller should call
        shutdown_sam3_service later), or None if the service was already running.
    """
    config_p = Path(config_path)
    if not config_p.is_absolute():
        config_p = Path(__file__).resolve().parent.parent / config_path
    if not config_p.exists():
        import logging

        logging.getLogger(__name__).error("Config not found: %s", config_p)
        return None

    cmd = [
        sys.executable,
        "-m",
        "services.sam3.server",
        "--host",
        host,
        "--port",
        str(port),
        "--config",
        str(config_p),
        "--device",
        device or resolve_device(),
    ]

    return ensure_service(
        "sam3",
        cmd,
        host,
        port,
        env=resolve_hf_env(),
        startup_timeout=startup_timeout,
    )


def shutdown_sam3_service(proc: Optional[subprocess.Popen]) -> None:
    """Shut down a SAM3 service subprocess that we started."""
    shutdown_service("sam3", proc)
