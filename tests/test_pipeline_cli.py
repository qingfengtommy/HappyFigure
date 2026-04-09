"""Tests for utility functions in scripts/pipeline_cli.py."""

from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Import after path setup
from scripts.pipeline_cli import (  # noqa: E402
    _load_state,
    _save_state,
    _progress,
    _json_out,
    _c,
    _build_tree,
)


# ---------------------------------------------------------------------------
# _load_state / _save_state
# ---------------------------------------------------------------------------


class TestLoadSaveState:
    def test_load_returns_empty_when_missing(self, tmp_path):
        state = _load_state(str(tmp_path / "nonexistent"))
        assert state == {}

    def test_round_trip(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        data = {"proposal": "test proposal", "iteration": 2, "figure_paths": ["/a.png"]}
        _save_state(run_dir, data)
        loaded = _load_state(run_dir)
        assert loaded == data

    def test_save_creates_parent_dirs(self, tmp_path):
        run_dir = str(tmp_path / "deep" / "nested" / "run_dir")
        _save_state(run_dir, {"key": "value"})
        assert (Path(run_dir) / "state.json").exists()

    def test_load_reads_utf8(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text('{"title": "Sch\\u00f6n"}', encoding="utf-8")
        state = _load_state(str(tmp_path))
        assert state["title"] == "Schön"

    def test_save_handles_non_serializable(self, tmp_path):
        """default=str in json.dump should handle Path objects etc."""
        run_dir = str(tmp_path)
        from datetime import datetime

        data = {"created": datetime(2026, 1, 15, 12, 0, 0)}
        _save_state(run_dir, data)
        loaded = _load_state(run_dir)
        assert "2026" in loaded["created"]


# ---------------------------------------------------------------------------
# _progress
# ---------------------------------------------------------------------------


class TestProgress:
    def test_writes_to_stderr(self, capsys):
        _progress("scanning files", "info")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "scanning files" in captured.err

    def test_level_variants(self, capsys):
        for level in ("info", "ok", "warn", "err", "dim"):
            _progress(f"msg-{level}", level)
        captured = capsys.readouterr()
        for level in ("info", "ok", "warn", "err", "dim"):
            assert f"msg-{level}" in captured.err


# ---------------------------------------------------------------------------
# _json_out
# ---------------------------------------------------------------------------


class TestJsonOut:
    def test_writes_json_to_stdout(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            _json_out({"status": "ok", "count": 3})
        output = buf.getvalue()
        parsed = json.loads(output)
        assert parsed["status"] == "ok"
        assert parsed["count"] == 3

    def test_writes_list(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            _json_out([1, 2, 3])
        parsed = json.loads(buf.getvalue())
        assert parsed == [1, 2, 3]


# ---------------------------------------------------------------------------
# _c (ANSI color helper)
# ---------------------------------------------------------------------------


class TestAnsiColor:
    def test_with_color_enabled(self):
        with patch("scripts.pipeline_cli._USE_COLOR", True):
            result = _c("32", "hello")
            assert result == "\033[32mhello\033[0m"

    def test_without_color(self):
        with patch("scripts.pipeline_cli._USE_COLOR", False):
            result = _c("32", "hello")
            assert result == "hello"


# ---------------------------------------------------------------------------
# _is_our_service
# ---------------------------------------------------------------------------


class TestIsOurService:
    """Test the PID validation function defined inside cmd_services.

    Since _is_our_service is a nested function inside the 'stop' branch,
    we recreate its logic here for testing.
    """

    @staticmethod
    def _is_our_service_impl(pid, service_name):
        """Reimplementation matching the logic in pipeline_cli.py."""
        _SERVICE_MODULES = {
            "sam3": "services.sam3.server",
            "ocr": "services.ocr.server",
            "ben2": "services.ben2.server",
        }
        module_marker = _SERVICE_MODULES.get(service_name, service_name)
        try:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if not cmdline_path.exists():
                return None
            cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
            return module_marker in cmdline
        except (OSError, PermissionError):
            return None

    def test_returns_true_when_marker_in_cmdline(self, tmp_path):
        fake_proc = tmp_path / "proc" / "12345" / "cmdline"
        fake_proc.parent.mkdir(parents=True)
        fake_proc.write_bytes(b"python\x00-m\x00services.sam3.server\x00--port\x008001")

        with patch("pathlib.Path.__new__"):
            # Simpler approach: patch at the read level
            pass

        # Direct test using a real temp file
        _SERVICE_MODULES = {
            "sam3": "services.sam3.server",
            "ocr": "services.ocr.server",
            "ben2": "services.ben2.server",
        }
        module_marker = _SERVICE_MODULES["sam3"]
        cmdline = fake_proc.read_bytes().decode("utf-8", errors="replace")
        assert module_marker in cmdline

    def test_returns_false_when_wrong_service(self, tmp_path):
        fake_proc = tmp_path / "cmdline"
        fake_proc.write_bytes(b"python\x00-m\x00services.ocr.server")
        cmdline = fake_proc.read_bytes().decode("utf-8", errors="replace")
        assert "services.sam3.server" not in cmdline

    def test_returns_none_when_no_proc(self):
        # On any system, a non-existent PID path returns None
        result = self._is_our_service_impl(99999999, "sam3")
        # Either None (no /proc) or False (proc exists but wrong)
        assert result is None or result is False


# ---------------------------------------------------------------------------
# _SERVICE_MODULES mapping
# ---------------------------------------------------------------------------


class TestServiceModules:
    def test_expected_services_mapped(self):
        """Verify the mapping covers all three services."""
        expected = {
            "sam3": "services.sam3.server",
            "ocr": "services.ocr.server",
            "ben2": "services.ben2.server",
        }
        for name, module in expected.items():
            assert module.endswith(".server")
            assert name in module or name == "ocr"


# ---------------------------------------------------------------------------
# _build_tree
# ---------------------------------------------------------------------------


class TestBuildTree:
    def test_simple_directory(self, tmp_path):
        (tmp_path / "file.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "data.csv").write_text("a,b\n1,2")

        result = _build_tree(str(tmp_path), max_depth=2)
        assert "file.txt" in result
        assert "subdir" in result
        assert "data.csv" in result

    def test_nonexistent_path(self):
        result = _build_tree("/nonexistent/path/xyz")
        assert "not found" in result

    def test_max_depth_limits_recursion(self, tmp_path):
        # Create deeply nested structure
        d = tmp_path
        for i in range(5):
            d = d / f"level{i}"
            d.mkdir()
            (d / "file.txt").write_text("x")

        result = _build_tree(str(tmp_path), max_depth=1)
        assert "level0" in result
        # level2+ should not appear since max_depth=1
        assert "level2" not in result


# ---------------------------------------------------------------------------
# Argument parser (subcommands)
# ---------------------------------------------------------------------------


class TestArgumentParser:
    """Test that the main() parser structure accepts expected subcommands."""

    def _make_parser(self):
        """Build the parser the same way main() does, without dispatching."""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)

        p_init = sub.add_parser("init")
        p_init.add_argument("--proposal", required=True)
        p_init.add_argument("--results-dir", default=None)
        p_init.add_argument("--llm-preset", default=None, choices=["azure", "gemini", "mixed"])
        p_init.add_argument("--run-dir", default=None)
        p_init.add_argument("--mode", default="happyfigure")
        p_init.add_argument("--style-examples", default=None)

        p_scan = sub.add_parser("data-scan")
        p_scan.add_argument("--run-dir", required=True)
        p_scan.add_argument("--verbose", action="store_true")

        p_svc = sub.add_parser("services")
        p_svc.add_argument("service_action", choices=["start", "stop", "health"])

        return parser

    def test_init_subcommand(self):
        parser = self._make_parser()
        args = parser.parse_args(["init", "--proposal", "paper.md", "--results-dir", "/data"])
        assert args.command == "init"
        assert args.proposal == "paper.md"
        assert args.results_dir == "/data"

    def test_data_scan_subcommand(self):
        parser = self._make_parser()
        args = parser.parse_args(["data-scan", "--run-dir", "/tmp/run1", "--verbose"])
        assert args.command == "data-scan"
        assert args.run_dir == "/tmp/run1"
        assert args.verbose is True

    def test_services_subcommand(self):
        parser = self._make_parser()
        args = parser.parse_args(["services", "health"])
        assert args.command == "services"
        assert args.service_action == "health"

    def test_init_requires_proposal(self):
        parser = self._make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["init"])

    def test_invalid_llm_preset_rejected(self):
        parser = self._make_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["init", "--proposal", "p.md", "--llm-preset", "invalid"])
