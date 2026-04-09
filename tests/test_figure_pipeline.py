"""Tests for utility functions in graphs/figure_pipeline.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure repo root is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Mock langgraph if not installed (it's a heavy dependency not needed for unit tests).
# StateGraph(SomeTypedDict) is called at module level and the returned graph object
# needs .add_node, .add_edge, .add_conditional_edges, .set_entry_point, .compile etc.
# Using a plain MagicMock (no spec) gives us all of those for free.
if "langgraph" not in sys.modules:
    _lg_graph_mod = MagicMock()
    # Make StateGraph a callable that returns a permissive MagicMock (no spec constraint)
    _lg_graph_mod.StateGraph = lambda *a, **kw: MagicMock()
    _lg_graph_mod.END = "END"
    sys.modules["langgraph"] = MagicMock()
    sys.modules["langgraph.graph"] = _lg_graph_mod


# ---------------------------------------------------------------------------
# _status_print
# ---------------------------------------------------------------------------


class TestStatusPrint:
    def test_writes_to_stderr(self, capsys):
        from graphs.figure_pipeline import _status_print

        _status_print("hello from test")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "hello from test" in captured.err

    def test_flushes_immediately(self):
        """_status_print uses flush=True so output appears without buffering."""
        from graphs.figure_pipeline import _status_print

        with patch("sys.stderr") as mock_stderr:
            _status_print("flush check")
            mock_stderr.write.assert_called()
            mock_stderr.flush.assert_called()


# ---------------------------------------------------------------------------
# FigurePipelineState structure
# ---------------------------------------------------------------------------


class TestFigurePipelineState:
    def test_is_typed_dict(self):
        from graphs.figure_pipeline import FigurePipelineState

        # TypedDict subclasses dict at runtime
        assert issubclass(FigurePipelineState, dict)

    def test_has_expected_keys(self):
        from graphs.figure_pipeline import FigurePipelineState

        annotations = FigurePipelineState.__annotations__
        expected_keys = [
            "proposal", "results_dir", "code", "figure_path",
            "critic_score", "iteration", "run_dir", "experiment_groups",
            "current_experiment_name", "figure_paths", "verbose",
            "beam_width", "run_mode",
        ]
        for key in expected_keys:
            assert key in annotations, f"Missing key: {key}"

    def test_total_false_allows_partial_instantiation(self):
        """FigurePipelineState uses total=False, so partial dicts are valid."""
        from graphs.figure_pipeline import FigurePipelineState

        # Should not raise — only a subset of keys provided
        state: FigurePipelineState = {"proposal": "test", "iteration": 1}
        assert state["proposal"] == "test"


# ---------------------------------------------------------------------------
# _artifact_dir
# ---------------------------------------------------------------------------


class TestArtifactDir:
    def test_basic_path(self, tmp_path):
        from graphs.figure_pipeline import _artifact_dir

        state = {
            "run_dir": str(tmp_path),
            "current_experiment_name": "exp1",
        }
        result = _artifact_dir(state)
        assert result == tmp_path / "exp1"
        assert result.is_dir()

    def test_default_name(self, tmp_path):
        from graphs.figure_pipeline import _artifact_dir

        state = {"run_dir": str(tmp_path)}
        result = _artifact_dir(state)
        assert result == tmp_path / "default"
        assert result.is_dir()

    def test_beam_tag_creates_subdir(self, tmp_path):
        from graphs.figure_pipeline import _artifact_dir

        state = {
            "run_dir": str(tmp_path),
            "current_experiment_name": "exp1",
            "beam_candidate_tag": "style0_code1",
        }
        result = _artifact_dir(state)
        assert result == tmp_path / "exp1" / "style0_code1"
        assert result.is_dir()

    def test_empty_beam_tag_ignored(self, tmp_path):
        from graphs.figure_pipeline import _artifact_dir

        state = {
            "run_dir": str(tmp_path),
            "current_experiment_name": "exp1",
            "beam_candidate_tag": "",
        }
        result = _artifact_dir(state)
        assert result == tmp_path / "exp1"


# ---------------------------------------------------------------------------
# _truncate_for_prompt
# ---------------------------------------------------------------------------


class TestTruncateForPrompt:
    def test_returns_empty_for_empty_input(self):
        from graphs.figure_pipeline import _truncate_for_prompt

        assert _truncate_for_prompt("", 100, "test") == ""

    def test_returns_unchanged_when_short(self):
        from graphs.figure_pipeline import _truncate_for_prompt

        text = "short text"
        assert _truncate_for_prompt(text, 100, "test") == text

    def test_truncates_long_text(self):
        from graphs.figure_pipeline import _truncate_for_prompt

        text = "a" * 500
        result = _truncate_for_prompt(text, 100, "data")
        assert len(result) <= 100
        assert "truncated" in result
        assert "data" in result

    def test_preserves_max_chars_limit(self):
        from graphs.figure_pipeline import _truncate_for_prompt

        text = "x" * 1000
        result = _truncate_for_prompt(text, 200, "label")
        assert len(result) <= 200


# ---------------------------------------------------------------------------
# _compact_experiment_group
# ---------------------------------------------------------------------------


class TestCompactExperimentGroup:
    def test_compacts_large_fields(self):
        from graphs.figure_pipeline import _compact_experiment_group, _TREE_CHARS_MAX

        group = {
            "name": "exp1",
            "tree": "t" * (_TREE_CHARS_MAX + 500),
            "schemas": "short",
            "statistics": "",
            "semantics": "",
        }
        result = _compact_experiment_group(group)
        assert len(result["tree"]) <= _TREE_CHARS_MAX
        assert result["schemas"] == "short"

    def test_preserves_other_keys(self):
        from graphs.figure_pipeline import _compact_experiment_group

        group = {"name": "exp1", "tree": "", "schemas": "", "statistics": "",
                 "semantics": "", "extra_key": "value"}
        result = _compact_experiment_group(group)
        assert result["extra_key"] == "value"

    def test_default_name_fallback(self):
        from graphs.figure_pipeline import _compact_experiment_group, _TREE_CHARS_MAX

        group = {"tree": "x" * (_TREE_CHARS_MAX + 100), "schemas": "",
                 "statistics": "", "semantics": ""}
        result = _compact_experiment_group(group)
        # Should not raise even without name
        assert "truncated" in result["tree"]


# ---------------------------------------------------------------------------
# _find_renderable_image
# ---------------------------------------------------------------------------


class TestFindRenderableImage:
    def test_returns_existing_png(self, tmp_path):
        from graphs.figure_pipeline import _find_renderable_image

        img = tmp_path / "test.png"
        img.write_bytes(b"fake png")
        assert _find_renderable_image(img) == img

    def test_returns_none_for_nonexistent(self, tmp_path):
        from graphs.figure_pipeline import _find_renderable_image

        assert _find_renderable_image(tmp_path / "missing.png") is None

    def test_finds_alternate_extension(self, tmp_path):
        from graphs.figure_pipeline import _find_renderable_image

        jpg = tmp_path / "test.jpg"
        jpg.write_bytes(b"fake jpg")
        # Ask for .svg (not a vision ext), but .jpg sibling exists
        result = _find_renderable_image(tmp_path / "test.svg")
        assert result == jpg

    def test_ignores_non_image_extension(self, tmp_path):
        from graphs.figure_pipeline import _find_renderable_image

        txt = tmp_path / "test.txt"
        txt.write_text("not an image")
        result = _find_renderable_image(txt)
        assert result is None


# ---------------------------------------------------------------------------
# _safe_image_data_url
# ---------------------------------------------------------------------------


class TestSafeImageDataUrl:
    def test_returns_valid_data_url(self):
        from graphs.figure_pipeline import _safe_image_data_url

        def encoder(p):
            return "data:image/png;base64,abc123"
        result = _safe_image_data_url(Path("test.png"), encoder)
        assert result == "data:image/png;base64,abc123"

    def test_returns_none_for_non_image(self):
        from graphs.figure_pipeline import _safe_image_data_url

        def encoder(p):
            return "data:text/html;base64,abc123"
        result = _safe_image_data_url(Path("test.html"), encoder)
        assert result is None

    def test_returns_none_on_encoder_exception(self):
        from graphs.figure_pipeline import _safe_image_data_url

        def bad_encoder(p):
            raise ValueError("encoding failed")

        result = _safe_image_data_url(Path("test.png"), bad_encoder)
        assert result is None


# ---------------------------------------------------------------------------
# _retry_llm_call
# ---------------------------------------------------------------------------


class TestRetryLlmCall:
    def test_returns_on_first_success(self):
        from graphs.figure_pipeline import _retry_llm_call

        result = _retry_llm_call(lambda: 42, max_attempts=3)
        assert result == 42

    def test_retries_on_failure(self):
        from graphs.figure_pipeline import _retry_llm_call

        call_count = 0

        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("fail")
            return "ok"

        with patch("time.sleep"):
            result = _retry_llm_call(flaky, max_attempts=3)
        assert result == "ok"
        assert call_count == 3

    def test_raises_after_max_attempts(self):
        from graphs.figure_pipeline import _retry_llm_call

        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="always fails"):
                _retry_llm_call(lambda: (_ for _ in ()).throw(RuntimeError("always fails")),
                                max_attempts=2)


# ---------------------------------------------------------------------------
# _report_progress (thread-safe callback)
# ---------------------------------------------------------------------------


class TestReportProgress:
    def test_calls_callback_when_set(self):
        import graphs.figure_pipeline as fp

        calls = []
        original = fp._parallel_progress_callback
        try:
            fp._parallel_progress_callback = lambda *a, **kw: calls.append((a, kw))
            fp._report_progress("exp1", "code_agent", 1, score=8.5)
            assert len(calls) == 1
            assert calls[0][0] == ("exp1", "code_agent", 1)
            assert calls[0][1] == {"score": 8.5}
        finally:
            fp._parallel_progress_callback = original

    def test_noop_when_no_callback(self):
        import graphs.figure_pipeline as fp

        original = fp._parallel_progress_callback
        try:
            fp._parallel_progress_callback = None
            # Should not raise
            fp._report_progress("exp1", "code_agent", 1)
        finally:
            fp._parallel_progress_callback = original
