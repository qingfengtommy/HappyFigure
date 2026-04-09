"""Agent subprocess execution — pipe, PTY, retry logic, doom-loop detection."""
from __future__ import annotations

import collections
import logging
import os
import pty
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Literal

import ui
from pipeline.context import PROJECT_ROOT, get_ctx
from ui.stream_parsers import (
    OpenCodeDbMonitor,
    dispatch_stream,
    sanitize_terminal_output,
    tee,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentLaunchRequest:
    """Higher-level agent launch contract above the raw subprocess primitive."""

    agent_name: str
    prompt: str
    verbose: bool = False
    log_dir: str | None = None
    log_name: str | None = None
    label: str | None = None
    role: Literal["session", "subagent"] = "subagent"


# ---------------------------------------------------------------------------
# Cross-session doom-loop detection
# ---------------------------------------------------------------------------


class FailurePatternDetector:
    """Detects repeated failure patterns across agent retries, scoped per agent.

    CLI platforms handle within-session doom loops.  This detector catches
    cross-session patterns: agent A fails, Python retries with same/similar
    prompt, agent A fails the same way.  Each launch is a fresh CLI session
    so the platform can't see the repetition.

    Errors are tracked per ``(agent_name)`` key so failures from unrelated
    agents (e.g. data-explore) don't leak into code-agent refinement prompts.
    """

    def __init__(self, window_size: int = 3) -> None:
        self._window_size = window_size
        self._recent_errors: dict[str, list[str]] = {}

    def record(self, agent_name: str, output_tail: str, exit_code: int) -> None:
        """Record a failed agent run for pattern detection."""
        if exit_code == 0:
            # Clear only this agent's history on success.
            self._recent_errors.pop(agent_name, None)
            return
        error_sig = _extract_error_signature(output_tail)
        if error_sig:
            errors = self._recent_errors.setdefault(agent_name, [])
            errors.append(error_sig)
            if len(errors) > self._window_size:
                self._recent_errors[agent_name] = errors[-self._window_size:]

    def detect(self, agent_name: str) -> str | None:
        """Return corrective guidance if a cross-session doom loop is detected
        for *agent_name*.  Returns ``None`` if no loop detected.
        """
        errors = self._recent_errors.get(agent_name, [])
        if len(errors) < 2:
            return None

        last = errors[-1]
        repeat_count = sum(1 for e in errors if e == last)
        if repeat_count >= 2:
            return (
                f"WARNING — Cross-session doom loop detected: the same error has "
                f"occurred {repeat_count} times across retries of {agent_name}.\n"
                f"Repeated error: {last}\n"
                f"You MUST try a fundamentally different approach — do NOT repeat "
                f"the same code structure or tool sequence that led to this error."
            )
        return None

    def clear(self, agent_name: str | None = None) -> None:
        """Reset errors for *agent_name*, or all agents if None."""
        if agent_name is None:
            self._recent_errors.clear()
        else:
            self._recent_errors.pop(agent_name, None)


def _extract_error_signature(output_tail: str) -> str:
    """Extract a short, distinctive error signature from agent output."""
    lines = output_tail.strip().splitlines()
    if not lines:
        return ""
    # Look for lines containing error keywords
    for line in reversed(lines):
        stripped = line.strip()
        if any(kw in stripped for kw in ("Error", "error", "ERROR", "Traceback", "FAILED")):
            # Normalise: strip ANSI, collapse whitespace, cap length
            clean = re.sub(r"\x1b\[[0-9;]*m", "", stripped)
            clean = " ".join(clean.split())
            return clean[:200]
    # Fallback: last non-empty line
    for line in reversed(lines):
        stripped = line.strip()
        if stripped:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", stripped)
            return " ".join(clean.split())[:200]
    return ""


# Module-level detector shared across retries within one orchestrator run.
_doom_detector = FailurePatternDetector()



def run_agent(
    agent_name: str,
    prompt: str,
    verbose: bool = False,
    log_dir: str | None = None,
    log_name: str | None = None,
    label: str | None = None,
    role: str = "subagent",
) -> int:
    """Launch an agent via the configured platform. Returns exit code.

    Args:
        label: Optional prefix for parallel output (e.g., experiment name).
               When set, all stdout from this agent is prefixed with [label].
        role: ``"session"`` for the main orchestrator, ``"subagent"`` for
              agents spawned within a pipeline stage.
    """
    ctx = get_ctx()
    orch = ctx.orchestrator

    if label:
        ui.set_agent_label(label)

    ac = orch.build_agent_command(agent_name, prompt)

    ui.agent_start(agent_name, orch.platform_name, orch.model_display, prompt, role=role)
    header = (
        f"Launching agent: {agent_name}\n"
        f"Platform: {orch.platform_name} · {orch.model_display}\n"
        f"Prompt: {prompt[:200]}{'...' if len(prompt) > 200 else ''}\n"
    )

    log_file = None
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        slug = log_name or agent_name
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", slug).strip("._") or agent_name
        log_path = os.path.join(log_dir, f"agent_{slug}.log")
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)  # line-buffered
        log_file.write(header)
        log_file.flush()

    env = None
    if ac.env:
        env = {**os.environ, **ac.env}

    opencode_monitor = None
    if ac.metadata and ac.metadata.get("opencode_db_path") and ac.metadata.get("opencode_session_title"):
        opencode_monitor = OpenCodeDbMonitor(
            ac.metadata["opencode_db_path"],
            ac.metadata["opencode_session_title"],
            log_file=log_file,
            label=label,
        )

    def _execute(agent_command) -> tuple[int, str]:
        output_tail = collections.deque(maxlen=200)
        if opencode_monitor is not None:
            opencode_monitor.output_tail = output_tail

        merged_env = env
        if agent_command.env:
            base = env if env is not None else os.environ
            merged_env = {**base, **agent_command.env}

        if agent_command.use_pty:
            return _execute_pty(agent_command, merged_env, output_tail)

        process = subprocess.Popen(
            agent_command.cmd,
            cwd=agent_command.cwd or str(PROJECT_ROOT),
            stdin=subprocess.PIPE if agent_command.input_text else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=merged_env,
        )
        try:
            if opencode_monitor is not None:
                opencode_monitor.start()
            if agent_command.input_text:
                process.stdin.write(agent_command.input_text)
                process.stdin.close()

            if agent_command.silent_stdout:
                raw_chunks: collections.deque[str] = collections.deque(maxlen=5000)

                def _drain_stdout():
                    try:
                        for line in process.stdout:
                            raw_chunks.append(line)
                    except OSError:
                        pass

                drain_thread = threading.Thread(target=_drain_stdout, name="silent-stdout-drain", daemon=True)
                drain_thread.start()
                process.wait()
                drain_thread.join(timeout=2.0)
                fallback = sanitize_terminal_output("".join(raw_chunks))
                if fallback:
                    if opencode_monitor is None or not opencode_monitor.saw_output:
                        tee(fallback + "\n", output_tail=output_tail, log_file=log_file)
                    else:
                        tee(fallback + "\n", output_tail, log_file, to_stdout=False)
                return process.returncode, "".join(output_tail)

            dispatch_stream(agent_command.stream_format, process.stdout, log_file, output_tail)
        except Exception as exc:
            logger.warning("Stream processing error: %s", exc)
        finally:
            try:
                remaining = process.stdout.read()
                if remaining and output_tail is not None:
                    output_tail.append(remaining)
            except OSError:
                pass
            process.wait()

        return process.returncode, "".join(output_tail)

    def _execute_pty(agent_command, merged_env, output_tail) -> tuple[int, str]:
        master_fd, slave_fd = pty.openpty()
        try:
            process = subprocess.Popen(
                agent_command.cmd,
                cwd=agent_command.cwd or str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                env=merged_env,
            )
        finally:
            os.close(slave_fd)

        if agent_command.stream_format in ("claude-stream-json", "opencode-json"):
            master_file = os.fdopen(master_fd, "r", encoding="utf-8", errors="replace")
            try:
                dispatch_stream(agent_command.stream_format, master_file, log_file, output_tail)
            except OSError:
                pass
            except Exception as exc:
                logger.warning("PTY stream error: %s", exc)
            finally:
                try:
                    master_file.close()
                except OSError:
                    pass
                process.wait()
        else:
            try:
                while True:
                    chunk = os.read(master_fd, 16384)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    output_tail.append(text)
                    ui.raw(text, log_file=log_file)
            except OSError:
                pass
            except Exception as exc:
                logger.warning("PTY stream error: %s", exc)
            finally:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                process.wait()

        return process.returncode, "".join(output_tail)

    try:
        rc, output_tail = _execute(ac)

        retry_checker = getattr(orch, "should_retry_with_dangerous_sandbox", None)
        retry_builder = getattr(orch, "build_fallback_agent_command", None)
        if (
            rc != 0
            and callable(retry_checker)
            and callable(retry_builder)
            and retry_checker(output_tail)
        ):
            retry_ac = retry_builder(prompt)
            if retry_ac is not None:
                ui.warn("Detected Codex sandbox bootstrap failure; retrying with danger-full-access")
                if log_file:
                    log_file.write("Detected Codex sandbox bootstrap failure; retrying\n")
                rc, output_tail = _execute(retry_ac)

        if log_file:
            log_file.write(f"\n[orchestrator] Agent {agent_name} exited with code {rc}\n")
    finally:
        if opencode_monitor is not None:
            opencode_monitor.stop()
            opencode_monitor.join(timeout=2.0)
        if log_file:
            log_file.close()

    if label:
        ui.set_agent_label(None)

    # Cross-session doom-loop tracking (scoped per agent name)
    _doom_detector.record(agent_name, output_tail, rc)
    if rc != 0:
        ui.warn(f"Agent {agent_name} exited with code {rc}")

    return rc


def execute_agent_request(request: AgentLaunchRequest) -> int:
    """Compatibility wrapper for higher-level orchestrator/runtime callers."""
    return run_agent(
        request.agent_name,
        request.prompt,
        verbose=request.verbose,
        log_dir=request.log_dir,
        log_name=request.log_name,
        label=request.label,
        role=request.role,
    )


def launch_orchestrator_session(
    agent_name: str,
    prompt: str,
    *,
    verbose: bool = False,
    log_dir: str | None = None,
    log_name: str | None = None,
    label: str | None = None,
) -> int:
    return execute_agent_request(
        AgentLaunchRequest(
            agent_name=agent_name,
            prompt=prompt,
            verbose=verbose,
            log_dir=log_dir,
            log_name=log_name,
            label=label,
            role="session",
        )
    )


def spawn_subagent(
    agent_name: str,
    prompt: str,
    *,
    verbose: bool = False,
    log_dir: str | None = None,
    log_name: str | None = None,
    label: str | None = None,
) -> int:
    return execute_agent_request(
        AgentLaunchRequest(
            agent_name=agent_name,
            prompt=prompt,
            verbose=verbose,
            log_dir=log_dir,
            log_name=log_name,
            label=label,
            role="subagent",
        )
    )


def get_doom_loop_guidance(agent_name: str = "code-agent") -> str | None:
    """Return corrective guidance if a cross-session doom loop is detected
    for *agent_name*.

    Called by beam refinement and retry paths to inject guidance into the
    next prompt.  Returns ``None`` when no loop is detected.
    """
    return _doom_detector.detect(agent_name)


def require_agent_success(agent_name: str, rc: int) -> None:
    if rc == 0:
        return
    ui.error(f"Agent {agent_name} failed with exit code {rc}")
    sys.exit(rc or 1)
