"""Auto-start and shutdown helpers for the PaddleOCR HTTP service.

Usage:
    from services.ocr.launcher import ensure_ocr_service, shutdown_ocr_service

    proc = ensure_ocr_service()
    # ... run pipeline ...
    shutdown_ocr_service(proc)
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from pipeline.service_launcher import ensure_service, shutdown_service

_DEFAULT_PORT = 8002
_DEFAULT_HOST = "127.0.0.1"


def ensure_ocr_service(
    host: str = _DEFAULT_HOST,
    port: int = _DEFAULT_PORT,
    lang: str = "en",
    startup_timeout: int = 120,
) -> Optional[subprocess.Popen]:
    """Ensure the OCR service is running. Start it if needed.

    Returns the Popen handle if we started it, or None if already running.
    """
    cmd = [
        sys.executable, "-m", "services.ocr.server",
        "--host", host,
        "--port", str(port),
        "--lang", lang,
    ]

    env = {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": os.environ.get(
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True"
    )}

    return ensure_service(
        "ocr", cmd, host, port,
        env=env,
        startup_timeout=startup_timeout,
    )


def shutdown_ocr_service(proc: Optional[subprocess.Popen]) -> None:
    """Shut down an OCR service subprocess that we started."""
    shutdown_service("ocr", proc)
