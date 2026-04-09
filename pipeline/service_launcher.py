"""Shared subprocess launcher for microservices (SAM3, OCR, BEN2).

Each service module provides a thin wrapper (``ensure_*_service`` /
``shutdown_*_service``) that builds the service-specific command and
delegates to this module for the common lifecycle logic.
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_HEALTH_TIMEOUT = 5
_DEFAULT_STARTUP_TIMEOUT = 120

# Guards per-service so shutdown is idempotent even under concurrent
# atexit + explicit shutdown calls.
_shutdown_locks: dict[str, threading.Lock] = {}
_shutdown_done: dict[str, bool] = {}


def health_check(host: str, port: int, *, timeout: int = _DEFAULT_HEALTH_TIMEOUT) -> bool:
    """Return True if a service at ``host:port`` responds to ``/health``."""
    try:
        resp = requests.get(f"http://{host}:{port}/health", timeout=timeout)
        return resp.status_code == 200
    except (requests.RequestException, OSError):
        return False


def ensure_service(
    name: str,
    cmd: list[str],
    host: str,
    port: int,
    *,
    env: dict[str, str] | None = None,
    startup_timeout: int = _DEFAULT_STARTUP_TIMEOUT,
) -> Optional[subprocess.Popen]:
    """Start a service subprocess if it is not already running.

    Args:
        name: Human-readable service name (for logging).
        cmd: Full command to start the service.
        host: Host to health-check.
        port: Port to health-check.
        env: Optional environment dict (merged with ``os.environ``).
        startup_timeout: Max seconds to wait for health check.

    Returns:
        The ``Popen`` handle if we started the service, or ``None`` if it
        was already running.  Pass the handle to :func:`shutdown_service`.
    """
    if health_check(host, port):
        logger.info("[%s] Service already running at %s:%d", name, host, port)
        return None

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    logger.info("[%s] Starting: %s", name, " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=merged_env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )

    atexit.register(lambda: _kill_proc(proc))

    logger.info("[%s] Waiting for service (timeout %ds)...", name, startup_timeout)
    deadline = time.time() + startup_timeout
    last_log_time = time.time()

    while time.time() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            _close_pipes(proc)
            logger.error("[%s] Exited with code %d", name, proc.returncode)
            if output:
                for line in output.strip().splitlines()[-30:]:
                    logger.error("[%s]   %s", name, line)
            return None

        if health_check(host, port):
            elapsed = time.time() - (deadline - startup_timeout)
            logger.info("[%s] Ready at %s:%d (%.1fs)", name, host, port, elapsed)
            return proc

        if time.time() - last_log_time > 15:
            elapsed = time.time() - (deadline - startup_timeout)
            logger.info("[%s] Still loading model... (%.0fs)", name, elapsed)
            last_log_time = time.time()

        time.sleep(2)

    logger.error("[%s] Timeout after %ds — killing", name, startup_timeout)
    _kill_proc(proc)
    return None


def shutdown_service(name: str, proc: Optional[subprocess.Popen]) -> None:
    """Shut down a service subprocess (idempotent, thread-safe)."""
    if proc is None:
        return

    lock = _shutdown_locks.setdefault(name, threading.Lock())
    with lock:
        if _shutdown_done.get(name, False):
            return
        _shutdown_done[name] = True

    _kill_proc(proc)
    logger.info("[%s] Service shut down", name)


def _kill_proc(proc: subprocess.Popen) -> None:
    """Gracefully terminate, then force-kill a subprocess."""
    if proc.poll() is not None:
        _close_pipes(proc)
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except OSError:
        pass
    finally:
        _close_pipes(proc)


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close subprocess pipes to prevent resource leaks."""
    for pipe in (proc.stdout, proc.stderr, proc.stdin):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def resolve_device() -> str:
    """Detect CUDA availability, defaulting to ``cpu``."""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


def resolve_hf_env() -> dict[str, str]:
    """Return env overrides to propagate HuggingFace cache location."""
    if "HF_HOME" in os.environ:
        return {}
    try:
        from huggingface_hub import constants
        return {"HF_HOME": str(Path(constants.HF_HUB_CACHE).parent)}
    except (ImportError, AttributeError):
        return {}
