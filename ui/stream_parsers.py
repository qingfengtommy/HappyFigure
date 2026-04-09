"""Stream parsers for agent platform JSON output.

Each agent CLI (Claude, OpenCode, Codex, Gemini, Copilot) emits a different
JSON streaming format.  The parsers here normalise those into unified
``ui.raw_tool_call()`` / ``ui.raw_tool_error()`` / ``ui.raw_error()`` calls
so that every platform renders the same structured ``⟡ tool: summary`` output.

Usage from ``cli.py``::

    from ui.stream_parsers import dispatch_stream
    dispatch_stream(agent_command.stream_format, process.stdout, log_file, output_tail)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import ui


# ---------------------------------------------------------------------------
# Known agent names (auto-discovered from prompts/agents/*.md)
# Used to distinguish subagent spawns from regular tool calls in platforms
# where agents are exposed as tools (Gemini, Copilot, Claude Code "Agent").
# ---------------------------------------------------------------------------


def _discover_agent_names() -> frozenset[str]:
    """Scan prompts/agents/ for known agent names."""
    prompts_dir = Path(__file__).resolve().parent / "prompts" / "agents"
    if prompts_dir.is_dir():
        return frozenset(p.stem for p in prompts_dir.glob("*.md"))
    return frozenset()


KNOWN_AGENTS: frozenset[str] = _discover_agent_names()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def truncate(text, limit: int | None = None) -> str:
    return ui.truncate_text(str(text), limit)


def normalize_opencode_event(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else event


def extract_error_message(err) -> str:
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
        for key in ("message", "name", "error"):
            if err.get(key):
                return str(err[key])
    return str(err or "unknown error")


def _infer_spinner_state(part: dict) -> str:
    """Infer idle spinner state from an OpenCode part dict."""
    ptype = part.get("type", "")
    if ptype == "text":
        return ui.IDLE_STATE_THINKING
    if ptype == "tool":
        st = part.get("state", "")
        # state can be a string ("pending"/"running"/"completed") or a dict with "status" key
        status = st if isinstance(st, str) else (st.get("status", "") if isinstance(st, dict) else "")
        if status in ("pending", "running"):
            return ui.IDLE_STATE_TOOL
    return ui.IDLE_STATE_WORKING


def _clean_bash_summary(cmd: str) -> str:
    """Clean up a bash command for display.

    - Heredocs (python3 - <<'PY' ...) → "python3 (inline script)"
    - Long python -c '...' → "python3 -c ..."
    - Strips shell noise while keeping the meaningful command.
    """
    cmd = cmd.strip()
    # Heredoc: python3 - <<'PY' ... or python3 -c '...'
    if re.match(r"python3?\s+(-\s*<<|.*<<)", cmd):
        return "python3 (inline script)"
    if re.match(r"python3?\s+-c\s+", cmd):
        # Keep the first meaningful part after -c
        match = re.match(r"python3?\s+-c\s+['\"]?(.*)", cmd)
        if match:
            snippet = match.group(1)[:60].rstrip("'\"")
            return f"python3 -c {snippet}…" if len(match.group(1)) > 60 else f"python3 -c {snippet}"
    # cat > file << EOF → "write: file"
    cat_match = re.match(r"cat\s*>\s*(\S+)", cmd)
    if cat_match:
        return f"write: {cat_match.group(1)}"
    return cmd


def summarize_tool_input(name: str, payload) -> str:
    if isinstance(payload, str):
        return truncate(payload)
    if not isinstance(payload, dict):
        return truncate(payload)

    lowered = name.lower()
    preferred_keys = (
        "command",
        "cmd",
        "file_path",
        "filePath",
        "path",
        "pattern",
        "description",
        "prompt",
        "query",
        "url",
        "message",
        "title",
        "name",
        "skill",
        "target_file",
    )
    for key in preferred_keys:
        value = payload.get(key)
        if value:
            value = str(value)
            # Clean up bash heredoc commands: extract the meaningful part
            if lowered == "bash" and key in ("command", "cmd"):
                value = _clean_bash_summary(value)
            return truncate(value)

    if lowered == "bash" and payload.get("raw"):
        return truncate(_clean_bash_summary(str(payload["raw"])))

    if isinstance(payload.get("todos"), list):
        todos = payload["todos"]
        done = sum(1 for todo in todos if isinstance(todo, dict) and todo.get("status") == "completed")
        _icons = {"completed": "✓", "in_progress": "▸", "pending": "·"}
        # Multiline: header line + one line per todo item
        lines = [f"{done}/{len(todos)}"]
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            icon = _icons.get(todo.get("status", "pending"), "·")
            content = " ".join((todo.get("content") or todo.get("title") or todo.get("description") or "?").split())
            lines.append(f"{icon} {content}")
        return "\n".join(lines)

    try:
        serialized = json.dumps(payload, ensure_ascii=False)
        # Don't show empty/trivial JSON — use a placeholder instead
        if serialized in ("{}", "[]", "null"):
            return ""
        return truncate(serialized)
    except TypeError:
        return truncate(payload)


def tee(text: str, output_tail=None, log_file=None, *, to_stdout: bool = True) -> None:
    """Write text to stdout (optionally), output_tail, and log_file."""
    if to_stdout:
        ui.raw(text)
    if output_tail is not None:
        output_tail.append(text)
    if log_file:
        log_file.write(text)


def sanitize_terminal_output(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# OpenCode helpers
# ---------------------------------------------------------------------------


class OpenCodeStreamState:
    def __init__(self):
        self.text_offsets: dict[str, int] = {}
        self.tool_status: dict[str, str] = {}
        self.subtasks_seen: set[str] = set()
        self.steps_seen: set[str] = set()
        self.patches_seen: set[str] = set()
        self.completed_messages: set[str] = set()
        self.message_errors_seen: set[str] = set()
        self.last_cost = None
        self.done_emitted = False

    def new_text_delta(self, part_id: str, text: str, explicit_delta: str | None = None) -> str:
        if explicit_delta is not None:
            self.text_offsets[part_id] = len(text)
            return explicit_delta
        offset = self.text_offsets.get(part_id, 0)
        if len(text) <= offset:
            return ""
        self.text_offsets[part_id] = len(text)
        return text[offset:]


def handle_opencode_part(
    part: dict, state: OpenCodeStreamState, *, output_tail=None, log_file=None, delta: str | None = None
) -> None:
    part_id = str(part.get("id", ""))
    ptype = part.get("type", "")

    if ptype == "text":
        text = part.get("text", "")
        new_text = state.new_text_delta(part_id, text, delta if isinstance(delta, str) else None)
        if new_text:
            tee(new_text, output_tail=output_tail, log_file=log_file)
        return

    if ptype == "tool":
        name = part.get("tool", "?")
        tool_state = part.get("state", {})
        if not isinstance(tool_state, dict):
            return
        status = tool_state.get("status", "")
        tool_input = tool_state.get("input") or tool_state.get("raw") or {}
        previous = state.tool_status.get(part_id)

        # OpenCode "task" tool is a subagent spawn — render with ▹ icon
        if name == "task" and isinstance(tool_input, dict):
            agent_name = tool_input.get("subagent_type") or tool_input.get("agent") or "subagent"
            summary = truncate(tool_input.get("description") or tool_input.get("prompt") or "")
            if status in {"pending", "running", "completed"}:
                if previous is None:
                    plain = ui.raw_subagent(agent_name, summary)
                    tee(plain, output_tail, log_file, to_stdout=False)
                state.tool_status[part_id] = status
                return
            if status == "error":
                plain = ui.raw_tool_error(f"{agent_name}: {extract_error_message(tool_state.get('error'))[:200]}")
                tee(plain, output_tail, log_file, to_stdout=False)
                state.tool_status[part_id] = status
                return

        summary = summarize_tool_input(name, tool_input)

        if status in {"pending", "running", "completed"}:
            if previous is None:
                plain = ui.raw_tool_call(name, summary)
                tee(plain, output_tail, log_file, to_stdout=False)
            state.tool_status[part_id] = status
            return

        if status == "error":
            plain = ui.raw_tool_error(extract_error_message(tool_state.get("error"))[:200])
            tee(plain, output_tail, log_file, to_stdout=False)
            state.tool_status[part_id] = status
            return

    if ptype in ("subtask", "agent"):
        if part_id not in state.subtasks_seen:
            # OpenCode 2.0+ uses "agent" (AgentPart) with .name;
            # legacy "subtask" (SubtaskPart) uses .agent / .description.
            agent_name = part.get("name") or part.get("agent") or "subagent"
            summary = (
                part.get("description")
                or part.get("prompt")
                or (part.get("source", {}).get("value", "") if isinstance(part.get("source"), dict) else "")
            )
            plain = ui.raw_subagent(agent_name, truncate(summary))
            tee(plain, output_tail, log_file, to_stdout=False)
            state.subtasks_seen.add(part_id)
        return

    if ptype == "step-start":
        if part_id not in state.steps_seen:
            title = part.get("snapshot") or "step"
            if not re.fullmatch(r"[0-9a-f]{32,64}", str(title)):
                plain = ui.raw_step(truncate(title, limit=80))
                tee(plain, output_tail, log_file, to_stdout=False)
            state.steps_seen.add(part_id)
        return

    if ptype == "patch":
        if part_id not in state.patches_seen:
            files = part.get("files", [])
            if isinstance(files, list) and files:
                plain = ui.raw_patch([str(f) for f in files])
                tee(plain, output_tail, log_file, to_stdout=False)
            state.patches_seen.add(part_id)
        return

    if ptype == "retry":
        plain = ui.raw_error(f"retry: {extract_error_message(part.get('error'))[:200]}")
        tee(plain, output_tail, log_file, to_stdout=False)


def handle_opencode_message(info: dict, state: OpenCodeStreamState, *, output_tail=None, log_file=None) -> None:
    if not isinstance(info, dict) or info.get("role") != "assistant":
        return
    if info.get("cost") is not None:
        state.last_cost = info.get("cost")
    err = info.get("error")
    message_id = str(info.get("id", ""))
    if err and message_id and message_id not in state.message_errors_seen:
        plain = ui.raw_error(extract_error_message(err)[:200])
        tee(plain, output_tail, log_file, to_stdout=False)
        state.message_errors_seen.add(message_id)
    time_info = info.get("time", {})
    if (
        isinstance(time_info, dict)
        and time_info.get("completed")
        and message_id
        and message_id not in state.completed_messages
    ):
        state.completed_messages.add(message_id)


class _ChildSessionTracker:
    """Tracks a child session (subagent) in the OpenCode DB."""

    def __init__(self, session_id: str, agent_name: str):
        self.session_id = session_id
        self.agent_name = agent_name
        self.state = OpenCodeStreamState()
        self.last_message_id: str | None = None
        self.last_part_id: str | None = None


class OpenCodeDbMonitor:
    def __init__(self, db_path: str, session_title: str, *, output_tail=None, log_file=None, label: str | None = None):
        self.db_path = db_path
        self.session_title = session_title
        self.output_tail = output_tail
        self.log_file = log_file
        self.label = label
        self.state = OpenCodeStreamState()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="opencode-db-monitor", daemon=True)
        self._last_message_id: str | None = None
        self._last_part_id: str | None = None
        # Child session tracking for subagent nesting
        self._child_sessions: dict[str, _ChildSessionTracker] = {}  # session_id → tracker
        self._pending_children: dict[str, str] = {}  # part_id → agent_name (awaiting session discovery)
        # Idle spinner — animated indicator when no new events arrive
        self.spinner = ui.IdleSpinner(label=label)

    def start(self) -> None:
        self.spinner.start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.spinner.stop()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    @property
    def saw_output(self) -> bool:
        return bool(
            self.state.text_offsets or self.state.tool_status or self.state.subtasks_seen or self.state.patches_seen
        )

    def _run(self) -> None:
        # Set thread-local label so output from this monitor thread is prefixed.
        if self.label:
            ui.set_agent_label(self.label)
        if not os.path.exists(self.db_path):
            return
        try:
            conn = sqlite3.connect(self.db_path, timeout=1.0, check_same_thread=False)
        except sqlite3.Error:
            return
        try:
            session_id = None
            while not self._stop.is_set():
                if session_id is None:
                    session_id = self._find_session_id(conn)
                if session_id:
                    self._poll_session(conn, session_id)
                    self._poll_child_sessions(conn)
                time.sleep(0.25)
            # Final poll
            if session_id:
                self._poll_session(conn, session_id)
                self._poll_child_sessions(conn)
        finally:
            conn.close()

    def _find_session_id(self, conn) -> str | None:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM session WHERE title = ? ORDER BY time_created DESC LIMIT 1",
            (self.session_title,),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None

    def _discover_child_sessions(self, conn) -> None:
        """Find new child sessions for pending subagent task tools."""
        if not self._pending_children:
            return
        cur = conn.cursor()
        # Match sessions whose title contains the agent name as a subagent marker
        for part_id, agent_name in list(self._pending_children.items()):
            # OpenCode task tool creates sessions titled like:
            #   "{description} (@{agent_name} subagent)" or just the agent name
            cur.execute(
                "SELECT id FROM session WHERE title LIKE ? ORDER BY time_created DESC LIMIT 1",
                (f"%@{agent_name}%",),
            )
            row = cur.fetchone()
            if row:
                sid = str(row[0])
                if sid not in self._child_sessions:
                    self._child_sessions[sid] = _ChildSessionTracker(sid, agent_name)
                del self._pending_children[part_id]

    def _poll_child_sessions(self, conn) -> None:
        """Poll all tracked child sessions and render their parts indented."""
        self._discover_child_sessions(conn)
        for tracker in self._child_sessions.values():
            self._poll_child(conn, tracker)

    def _poll_child(self, conn, tracker: _ChildSessionTracker) -> None:
        """Poll a single child session, rendering tool calls with nesting indent."""
        cur = conn.cursor()
        indent = 2  # extra indent for subagent tool calls

        # Parts only (skip text — subagent text is usually internal reasoning)
        part_base = """
            SELECT part.id, part.data, message.data
            FROM part
            JOIN message ON message.id = part.message_id
            WHERE part.session_id = ?"""
        part_order = " ORDER BY part.time_created, part.id"
        if tracker.last_part_id:
            cur.execute(f"{part_base} AND part.id > ?{part_order}", (tracker.session_id, tracker.last_part_id))
        else:
            cur.execute(f"{part_base}{part_order}", (tracker.session_id,))
        for part_id, data, message_data in cur.fetchall():
            tracker.last_part_id = part_id
            try:
                message = json.loads(message_data)
            except json.JSONDecodeError:
                message = {}
            if message.get("role") != "assistant":
                continue
            try:
                part = json.loads(data)
            except json.JSONDecodeError:
                continue
            part.setdefault("id", part_id)
            ptype = part.get("type", "")
            # Only render tool calls and patches from child sessions (skip text, subtask etc.)
            if ptype == "tool":
                name = part.get("tool", "?")
                tool_state = part.get("state", {})
                if not isinstance(tool_state, dict):
                    continue
                status = tool_state.get("status", "")
                tool_input = tool_state.get("input") or tool_state.get("raw") or {}
                prev = tracker.state.tool_status.get(part_id)
                # Skip nested task tools (sub-subagents) to avoid deep recursion
                if name == "task":
                    continue
                if status in ("pending", "running", "completed") and prev is None:
                    summary = summarize_tool_input(name, tool_input)
                    plain = ui.raw_tool_call(name, summary, indent=indent)
                    tee(plain, self.output_tail, self.log_file, to_stdout=False)
                elif status == "error":
                    plain = ui.raw_tool_error(extract_error_message(tool_state.get("error"))[:200])
                    tee(plain, self.output_tail, self.log_file, to_stdout=False)
                tracker.state.tool_status[part_id] = status

    def _poll_session(self, conn, session_id: str) -> None:
        cur = conn.cursor()

        # Fetch messages incrementally
        msg_base = "SELECT id, data FROM message WHERE session_id = ?"
        msg_order = " ORDER BY time_created, id"
        if self._last_message_id:
            cur.execute(f"{msg_base} AND id > ?{msg_order}", (session_id, self._last_message_id))
        else:
            cur.execute(f"{msg_base}{msg_order}", (session_id,))
        for message_id, data in cur.fetchall():
            self._last_message_id = message_id
            try:
                info = json.loads(data)
            except json.JSONDecodeError:
                continue
            info.setdefault("id", message_id)
            handle_opencode_message(info, self.state, output_tail=self.output_tail, log_file=self.log_file)

        # Fetch parts incrementally
        part_base = """
            SELECT part.id, part.data, message.data
            FROM part
            JOIN message ON message.id = part.message_id
            WHERE part.session_id = ?"""
        part_order = " ORDER BY part.time_created, part.id"
        if self._last_part_id:
            cur.execute(f"{part_base} AND part.id > ?{part_order}", (session_id, self._last_part_id))
        else:
            cur.execute(f"{part_base}{part_order}", (session_id,))
        for part_id, data, message_data in cur.fetchall():
            self._last_part_id = part_id
            try:
                message = json.loads(message_data)
            except json.JSONDecodeError:
                message = {}
            if message.get("role") != "assistant":
                continue
            try:
                part = json.loads(data)
            except json.JSONDecodeError:
                continue
            part.setdefault("id", part_id)
            self.spinner.notify(state=_infer_spinner_state(part))
            # Track task tool spawns for child session discovery
            if part.get("type") == "tool" and part.get("tool") == "task":
                tool_state = part.get("state", {})
                if isinstance(tool_state, dict):
                    tool_input = tool_state.get("input") or {}
                    if isinstance(tool_input, dict):
                        agent_name = tool_input.get("subagent_type") or tool_input.get("agent")
                        if agent_name and part_id not in self._pending_children:
                            self._pending_children[part_id] = agent_name
            handle_opencode_part(part, self.state, output_tail=self.output_tail, log_file=self.log_file)


# ---------------------------------------------------------------------------
# Platform stream parsers
# ---------------------------------------------------------------------------


def stream_claude_json(stdout, log_file=None, output_tail=None):
    """Parse Claude CLI stream-json output and display intermediate steps.

    Shows tool calls (name + truncated input) and assistant text as they arrive,
    similar to how OpenCode streams intermediate steps.
    """
    spinner = ui.IdleSpinner()
    spinner.start()

    def _emit(text: str) -> None:
        tee(text, output_tail)

    try:
        for raw_line in stdout:
            if log_file:
                log_file.write(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                _emit(raw_line)
                continue

            etype = event.get("type")

            if etype == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    btype = block.get("type")
                    if btype == "text":
                        spinner.notify(state=ui.IDLE_STATE_THINKING)
                        ui.thinking_start()
                        _emit(block.get("text", ""))
                    elif btype == "tool_use":
                        spinner.notify(state=ui.IDLE_STATE_TOOL)
                        ui.thinking_end()
                        name = block.get("name", "?")
                        inp = block.get("input", {})
                        # Claude Code "Agent" tool → render as subagent
                        if name == "Agent" and isinstance(inp, dict):
                            agent_name = inp.get("skill") or inp.get("name") or inp.get("subagent_type") or "subagent"
                            summary = truncate(inp.get("description") or inp.get("prompt") or "")
                            plain = ui.raw_subagent(agent_name, summary)
                            tee(plain, output_tail, to_stdout=False)
                        else:
                            summary = summarize_tool_input(name, inp)
                            plain = ui.raw_tool_call(name, summary)
                            tee(plain, output_tail, to_stdout=False)
                    elif btype == "thinking":
                        spinner.notify(state=ui.IDLE_STATE_THINKING)

            elif etype == "user":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                ui.thinking_end()
                msg = event.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("is_error"):
                            err_text = block.get("content", "")
                            if isinstance(err_text, str) and err_text.strip():
                                plain = ui.raw_tool_error(err_text[:200])
                                tee(plain, output_tail, to_stdout=False)

            elif etype == "result":
                spinner.stop()  # final event — stop thread, don't just notify
                ui.thinking_end()
                result_text = event.get("result", "")
                if result_text:
                    _emit(result_text)
                is_error = event.get("is_error", False)
                subtype = event.get("subtype", "")
                if is_error or subtype.startswith("error"):
                    err = event.get("error", "") or subtype or "unknown error"
                    plain = ui.raw_error(f"[claude] {err}")
                    tee(plain, output_tail, to_stdout=False)
                # Summary footer
                cost = event.get("total_cost_usd")
                turns = event.get("num_turns", "?")
                duration = event.get("duration_ms", 0)
                in_tok = event.get("total_input_tokens", 0) or 0
                out_tok = event.get("total_output_tokens", 0) or 0
                ui.agent_done(turns=turns, duration_ms=duration, cost=cost, input_tokens=in_tok, output_tokens=out_tok)
    finally:
        spinner.stop()


def stream_opencode_json(stdout, log_file=None, output_tail=None):
    """Parse OpenCode ``--format json`` output and display intermediate steps.

    OpenCode JSON events (one per line):
        type=text-delta      : streaming text chunk
        type=tool-call       : tool invocation (name + arguments)
        type=tool-result     : tool output/result
        type=step-start      : agent step boundary
        type=step-finish     : agent step completed
        type=error           : error event
        type=message.*       : message lifecycle events
    """
    state = OpenCodeStreamState()
    spinner = ui.IdleSpinner()
    spinner.start()

    def _emit(text: str) -> None:
        tee(text, output_tail)

    try:
        for raw_line in stdout:
            if log_file:
                log_file.write(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                _emit(raw_line)
                continue

            event = normalize_opencode_event(event)
            etype = event.get("type", "")
            props = event.get("properties", {}) if isinstance(event.get("properties"), dict) else {}

            # Current OpenCode event schema (SDK-backed)
            if etype == "message.part.updated":
                part = props.get("part", {})
                if not isinstance(part, dict):
                    continue
                spinner.notify(state=_infer_spinner_state(part))
                handle_opencode_part(
                    part,
                    state,
                    output_tail=output_tail,
                    log_file=log_file,
                    delta=props.get("delta") if isinstance(props.get("delta"), str) else None,
                )
                continue

            elif etype == "message.updated":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                info = props.get("info", {})
                handle_opencode_message(info, state, output_tail=output_tail, log_file=log_file)
                continue

            elif etype in ("session.idle",):
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                if not state.done_emitted:
                    ui.agent_done(cost=state.last_cost)
                    state.done_emitted = True
                continue

            elif etype == "session.status":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                status = props.get("status", {})
                if isinstance(status, dict) and status.get("type") == "idle" and not state.done_emitted:
                    ui.agent_done(cost=state.last_cost)
                    state.done_emitted = True
                continue

            elif etype == "session.error":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                plain = ui.raw_error(extract_error_message(props.get("error"))[:200])
                tee(plain, output_tail, to_stdout=False)
                continue

            if etype == "text-delta":
                spinner.notify(state=ui.IDLE_STATE_THINKING)
                # Streaming text chunk from assistant
                delta = event.get("textDelta", "") or event.get("delta", "") or event.get("text", "")
                if delta:
                    ui.thinking_start()
                    _emit(delta)

            elif etype == "tool-call":
                spinner.notify(state=ui.IDLE_STATE_TOOL)
                ui.thinking_end()
                name = event.get("toolName", "") or event.get("name", "?")
                args = event.get("args", {})
                summary = summarize_tool_input(name, args)
                plain = ui.raw_tool_call(name, summary)
                tee(plain, output_tail, to_stdout=False)

            elif etype == "tool-result":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                ui.thinking_end()
                # Tool output — show truncated result
                content = event.get("result", "") or event.get("content", "") or event.get("text", "")
                if isinstance(content, list):
                    # Array of content blocks
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text", "")
                            if text:
                                _emit(text[:500] + "\n" if len(text) > 500 else text + "\n")
                elif isinstance(content, str) and content.strip():
                    _emit(content[:500] + "\n" if len(content) > 500 else content + "\n")

            elif etype == "error":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                err = event.get("error", {})
                if isinstance(err, dict):
                    # OpenCode nests: error.data.message or error.message
                    data = err.get("data", {})
                    if isinstance(data, dict):
                        msg = data.get("message", "")
                    else:
                        msg = ""
                    msg = msg or err.get("message", "") or err.get("name", "unknown error")
                else:
                    msg = str(err)
                plain = ui.raw_error(msg[:200])
                tee(plain, output_tail, to_stdout=False)

            elif etype == "step-start":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                agent = event.get("agent", "") or event.get("name", "")
                if agent:
                    _emit(f"\n> {agent}\n\n")

            elif etype in ("message.completed", "message.finish", "done", "complete"):
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                # Session completed — extract summary info if available
                usage = event.get("usage", {}) or event.get("totalUsage", {})
                if usage:
                    in_tok = usage.get("promptTokens", 0) or usage.get("input_tokens", 0)
                    out_tok = usage.get("completionTokens", 0) or usage.get("output_tokens", 0)
                    total = usage.get("totalTokens", 0) or (in_tok + out_tok)
                    if total:
                        ui.agent_done(input_tokens=in_tok, output_tokens=out_tok)
                        state.done_emitted = True
    finally:
        spinner.stop()


def _extract_codex_agent_name(prompt: str) -> str:
    """Try to extract agent name from a Codex spawn_agent prompt."""
    # Prompts typically start with "Act as @agent-name ..."
    import re

    m = re.search(r"@([\w-]+)", prompt[:200])
    return m.group(1) if m else "subagent"


def stream_codex_json(stdout, log_file=None, output_tail=None):
    """Parse Codex CLI ``--json`` JSONL output and display intermediate steps.

    Codex JSONL events:
        type=item.started  : tool invocation start (command_execution, collab_tool_call)
        type=item.completed: tool result or agent message
        type=turn.completed: turn summary with usage
    """
    spinner = ui.IdleSpinner()
    spinner.start()

    try:
        for raw_line in stdout:
            if log_file:
                log_file.write(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                tee(raw_line, output_tail)
                continue

            etype = event.get("type", "")
            item = event.get("item", {})

            if etype == "item.started":
                spinner.notify(state=ui.IDLE_STATE_TOOL)
                itype = item.get("type", "")
                if itype in ("agent_handoff", "collab_tool_call"):
                    # Codex native agent delegation (collab_tool_call with
                    # tool=spawn_agent) or legacy agent_handoff events.
                    tool = item.get("tool", "")
                    prompt = item.get("prompt", "") or item.get("description", "")
                    if tool == "spawn_agent" or itype == "agent_handoff":
                        agent_name = item.get("agent", "") or item.get("name", "") or _extract_codex_agent_name(prompt)
                        summary = truncate(prompt)
                        plain = ui.raw_subagent(agent_name, summary)
                        tee(plain, output_tail, to_stdout=False)
                    else:
                        # Other collab tools (e.g. future Codex tools)
                        plain = ui.raw_tool_call(tool or "collab", truncate(prompt))
                        tee(plain, output_tail, to_stdout=False)
                elif itype == "command_execution":
                    cmd = item.get("command", "")
                    # Strip shell prefix like "/bin/bash -lc "
                    if " -lc " in cmd:
                        cmd = cmd.split(" -lc ", 1)[1]
                    plain = ui.raw_tool_call("bash", truncate(cmd))
                    tee(plain, output_tail, to_stdout=False)

            elif etype == "item.completed":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                itype = item.get("type", "")
                if itype == "agent_message":
                    text = item.get("text", "")
                    if text:
                        # Style thinking/reasoning text as dim italic
                        plain = ui.raw_thinking(text)
                        tee(plain, output_tail, to_stdout=False)
                elif itype in ("agent_handoff", "collab_tool_call"):
                    # Subagent completion — check for status updates
                    states = item.get("agents_states", {})
                    for _tid, st in states.items():
                        msg = st.get("message")
                        if msg:
                            tee(msg + "\n", output_tail)
                elif itype == "command_execution":
                    # Tool completed — show truncated output
                    exit_code = item.get("exit_code")
                    if exit_code and exit_code != 0:
                        plain = ui.raw_tool_error(f"exit code {exit_code}")
                        tee(plain, output_tail, to_stdout=False)

            elif etype == "turn.completed":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                usage = event.get("usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                if in_tok or out_tok:
                    ui.agent_done(input_tokens=in_tok, output_tokens=out_tok)

            elif etype == "error":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                msg = event.get("message", "unknown error")
                plain = ui.raw_error(f"[codex] {truncate(msg, 200)}")
                tee(plain, output_tail, to_stdout=False)

            elif etype == "turn.failed":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                err = event.get("error", {})
                msg = err.get("message", "turn failed") if isinstance(err, dict) else str(err)
                plain = ui.raw_error(f"[codex] {truncate(msg, 200)}")
                tee(plain, output_tail, to_stdout=False)
    finally:
        spinner.stop()


def stream_gemini_json(stdout, log_file=None, output_tail=None):
    """Parse Gemini CLI ``-o stream-json`` output and display intermediate steps.

    Gemini stream-json events:
        type=init       : session start with model info
        type=message    : user/assistant message (with optional delta)
        type=tool_call  : tool invocation
        type=tool_result: tool output
        type=result     : session summary
    """
    spinner = ui.IdleSpinner()
    spinner.start()

    def _emit(text: str) -> None:
        tee(text, output_tail)

    try:
        for raw_line in stdout:
            if log_file:
                log_file.write(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                _emit(raw_line)
                continue

            etype = event.get("type", "")

            if etype == "message":
                spinner.notify(state=ui.IDLE_STATE_THINKING)
                role = event.get("role", "")
                content = event.get("content", "")
                if role == "assistant" and content:
                    plain = ui.raw_thinking(content)
                    tee(plain, output_tail, to_stdout=False)

            elif etype == "tool_call":
                spinner.notify(state=ui.IDLE_STATE_TOOL)
                name = event.get("name", "") or event.get("tool", "?")
                args = event.get("arguments", {}) or event.get("args", {})
                # Gemini exposes agents as tools — detect by name
                if name in KNOWN_AGENTS:
                    summary = truncate(
                        (args.get("prompt") or args.get("description") or "") if isinstance(args, dict) else ""
                    )
                    plain = ui.raw_subagent(name, summary)
                else:
                    summary = summarize_tool_input(name, args)
                    plain = ui.raw_tool_call(name, summary)
                tee(plain, output_tail, to_stdout=False)

            elif etype == "tool_result":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                # Optionally show errors
                error = event.get("error")
                if error:
                    plain = ui.raw_tool_error(str(error)[:200])
                    tee(plain, output_tail, to_stdout=False)

            elif etype == "result":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                stats = event.get("stats", {})
                duration = stats.get("duration_ms")
                usage = event.get("usage", stats.get("usage", {}))
                in_tok = usage.get("input_tokens", 0) or usage.get("promptTokens", 0) or 0
                out_tok = usage.get("output_tokens", 0) or usage.get("completionTokens", 0) or 0
                ui.agent_done(duration_ms=duration, input_tokens=in_tok, output_tokens=out_tok)

            elif etype == "error":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                msg = event.get("message", "") or event.get("error", "unknown error")
                plain = ui.raw_error(f"[gemini] {str(msg)[:200]}")
                tee(plain, output_tail, to_stdout=False)
    finally:
        spinner.stop()


def stream_copilot_json(stdout, log_file=None, output_tail=None):
    """Parse Copilot CLI ``--output-format json`` JSONL output.

    Copilot JSONL events:
        type=assistant.message_delta : streaming text chunk
        type=assistant.message       : complete message (may include toolRequests)
        type=tool.execution_start    : tool invocation
        type=tool.execution_complete : tool result
        type=result                  : session summary
    """
    spinner = ui.IdleSpinner()
    spinner.start()

    def _emit(text: str) -> None:
        tee(text, output_tail)

    try:
        for raw_line in stdout:
            if log_file:
                log_file.write(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                _emit(raw_line)
                continue

            etype = event.get("type", "")
            data = event.get("data", {})

            if etype == "assistant.message_delta":
                spinner.notify(state=ui.IDLE_STATE_THINKING)
                delta = data.get("deltaContent", "")
                if delta:
                    ui.thinking_start()
                    _emit(delta)

            elif etype == "tool.execution_start":
                spinner.notify(state=ui.IDLE_STATE_TOOL)
                ui.thinking_end()
                name = data.get("toolName", "?")
                args = data.get("arguments", {})
                # Copilot exposes agents as tools — detect by name
                if name in KNOWN_AGENTS:
                    summary = truncate(
                        (args.get("prompt") or args.get("description") or "") if isinstance(args, dict) else ""
                    )
                    plain = ui.raw_subagent(name, summary)
                else:
                    summary = summarize_tool_input(name, args)
                    plain = ui.raw_tool_call(name, summary)
                tee(plain, output_tail, to_stdout=False)

            elif etype == "tool.execution_complete":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                success = data.get("success", True)
                if not success:
                    result = data.get("result", {})
                    msg = result.get("content", "tool failed") if isinstance(result, dict) else str(result)
                    plain = ui.raw_tool_error(str(msg)[:200])
                    tee(plain, output_tail, to_stdout=False)

            elif etype == "assistant.message":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                ui.thinking_end()
                content = data.get("content", "")
                phase = data.get("phase", "")
                if content and phase == "final_answer":
                    _emit(content + "\n")

            elif etype == "result":
                spinner.notify(state=ui.IDLE_STATE_WORKING)
                ui.thinking_end()
                usage = data.get("usage", event.get("usage", {}))
                duration = usage.get("sessionDurationMs") or usage.get("totalApiDurationMs")
                in_tok = usage.get("inputTokens", 0) or usage.get("promptTokens", 0) or 0
                out_tok = usage.get("outputTokens", 0) or usage.get("completionTokens", 0) or 0
                ui.agent_done(duration_ms=duration, input_tokens=in_tok, output_tokens=out_tok)
    finally:
        spinner.stop()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_STREAM_PARSERS = {
    "claude-stream-json": stream_claude_json,
    "opencode-json": stream_opencode_json,
    "codex-json": stream_codex_json,
    "gemini-stream-json": stream_gemini_json,
    "copilot-json": stream_copilot_json,
}


def dispatch_stream(stream_format, stdout, log_file=None, output_tail=None):
    """Route to the correct stream parser, or fall back to raw passthrough."""
    parser = _STREAM_PARSERS.get(stream_format)
    if parser:
        parser(stdout, log_file, output_tail)
    else:
        for line in stdout:
            if output_tail is not None:
                output_tail.append(line)
            ui.raw(line, log_file=log_file)
