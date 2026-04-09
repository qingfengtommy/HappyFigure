"""Tests for tools/critic_tools.py and tools/tool_schemas.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.critic_tools import _validate_review, _DIMENSION_RANGES, execute_critic_tool  # noqa: E402
from tools.tool_schemas import (  # noqa: E402
    DATA_TOOL_SCHEMAS,
    CRITIC_TOOL_SCHEMAS,
    CODE_TOOL_SCHEMAS,
    to_openai_tools,
    _convert_schema_for_gemini,
)


# ---------------------------------------------------------------------------
# Helper: build a valid review dict
# ---------------------------------------------------------------------------

def _valid_review(**overrides):
    base = {
        "score": 8.0,
        "verdict": "ACCEPT",
        "dimension_scores": {
            "data_accuracy": 2.0,
            "clarity": 2.0,
            "accessibility": 2.0,
            "layout": 2.0,
            "publication_readiness": 2.0,
            "confusion_penalty": -2.0,
        },
        "strengths": ["clear layout", "accurate data"],
        "issues": [{"description": "minor spacing issue"}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _validate_review — valid cases
# ---------------------------------------------------------------------------


class TestValidateReviewValid:
    def test_fully_valid_review(self):
        assert _validate_review(_valid_review()) is None

    def test_zero_score_valid(self):
        dims = {k: 0 for k in _DIMENSION_RANGES}
        dims["confusion_penalty"] = 0
        review = _valid_review(score=0.0, verdict="NEEDS_IMPROVEMENT",
                               dimension_scores=dims)
        assert _validate_review(review) is None

    def test_max_score_valid(self):
        dims = {
            "data_accuracy": 2.0,
            "clarity": 2.0,
            "accessibility": 2.0,
            "layout": 2.0,
            "publication_readiness": 2.0,
            "confusion_penalty": 0,
        }
        review = _valid_review(score=10.0, verdict="ACCEPT",
                               dimension_scores=dims)
        assert _validate_review(review) is None

    def test_empty_issues_list(self):
        review = _valid_review(issues=[])
        assert _validate_review(review) is None

    def test_issue_with_optional_fields(self):
        review = _valid_review(issues=[{
            "description": "font too small",
            "code_snippet": "plt.xlabel('x', fontsize=4)",
            "fix_suggestion": "Use fontsize=8",
        }])
        assert _validate_review(review) is None


# ---------------------------------------------------------------------------
# _validate_review — missing fields
# ---------------------------------------------------------------------------


class TestValidateReviewMissing:
    @pytest.mark.parametrize("field", ["score", "verdict", "dimension_scores", "strengths", "issues"])
    def test_missing_required_field(self, field):
        review = _valid_review()
        del review[field]
        err = _validate_review(review)
        assert err is not None
        assert "Missing required field" in err["error"]
        assert field in err["error"]

    def test_missing_dimension(self):
        review = _valid_review()
        del review["dimension_scores"]["clarity"]
        err = _validate_review(review)
        assert err is not None
        assert "Missing dimension score" in err["error"]


# ---------------------------------------------------------------------------
# _validate_review — invalid values
# ---------------------------------------------------------------------------


class TestValidateReviewInvalid:
    def test_score_out_of_range_high(self):
        review = _valid_review(score=11.0)
        err = _validate_review(review)
        assert err is not None
        assert "Score must be 0-10" in err["error"]

    def test_score_out_of_range_negative(self):
        review = _valid_review(score=-1)
        err = _validate_review(review)
        assert err is not None

    def test_score_not_a_number(self):
        review = _valid_review(score="high")
        err = _validate_review(review)
        assert err is not None

    def test_invalid_verdict(self):
        review = _valid_review(verdict="REJECT")
        err = _validate_review(review)
        assert err is not None
        assert "Verdict must be" in err["error"]

    def test_dimension_out_of_range(self):
        review = _valid_review()
        review["dimension_scores"]["clarity"] = 5.0
        err = _validate_review(review)
        assert err is not None
        assert "clarity" in err["error"]

    def test_confusion_penalty_positive(self):
        review = _valid_review()
        review["dimension_scores"]["confusion_penalty"] = 1.0
        err = _validate_review(review)
        assert err is not None
        assert "confusion_penalty" in err["error"]

    def test_score_dimension_sum_mismatch(self):
        review = _valid_review(score=5.0)  # sum of dims is 8.0
        err = _validate_review(review)
        assert err is not None
        assert "sum of dimensions" in err["error"]

    def test_strengths_not_a_list(self):
        review = _valid_review(strengths="good")
        err = _validate_review(review)
        assert err is not None
        assert "strengths must be a list" in err["error"]

    def test_issues_not_a_list(self):
        review = _valid_review(issues="bad axis")
        err = _validate_review(review)
        assert err is not None
        assert "issues must be a list" in err["error"]

    def test_issue_item_not_a_dict(self):
        review = _valid_review(issues=["string issue"])
        err = _validate_review(review)
        assert err is not None
        assert "must be a dict" in err["error"]

    def test_issue_missing_description(self):
        review = _valid_review(issues=[{"fix_suggestion": "do X"}])
        err = _validate_review(review)
        assert err is not None
        assert "missing required 'description'" in err["error"]


# ---------------------------------------------------------------------------
# _DIMENSION_RANGES
# ---------------------------------------------------------------------------


class TestDimensionRanges:
    def test_has_six_dimensions(self):
        assert len(_DIMENSION_RANGES) == 6

    def test_positive_dimensions_range_0_2(self):
        for dim, (lo, hi) in _DIMENSION_RANGES.items():
            if dim != "confusion_penalty":
                assert lo == 0 and hi == 2, f"{dim} range unexpected"

    def test_confusion_penalty_range(self):
        lo, hi = _DIMENSION_RANGES["confusion_penalty"]
        assert lo == -2
        assert hi == 0


# ---------------------------------------------------------------------------
# execute_critic_tool
# ---------------------------------------------------------------------------


class TestExecuteCriticTool:
    def test_valid_submission(self):
        review = _valid_review()
        result = execute_critic_tool("submit_review", review)
        assert result["status"] == "accepted"
        assert result["score"] == 8.0
        assert result["verdict"] == "ACCEPT"

    def test_unknown_tool(self):
        result = execute_critic_tool("unknown_tool", {})
        assert "error" in result
        assert "Unknown critic tool" in result["error"]

    def test_non_dict_args(self):
        result = execute_critic_tool("submit_review", "not a dict")
        assert "error" in result
        assert "Expected dict" in result["error"]

    def test_invalid_review_returns_error(self):
        result = execute_critic_tool("submit_review", {"score": 5})
        assert "error" in result


# ---------------------------------------------------------------------------
# tool_schemas — schema structure
# ---------------------------------------------------------------------------


class TestToolSchemas:
    def test_data_tool_schemas_count(self):
        assert len(DATA_TOOL_SCHEMAS) == 4

    def test_critic_tool_schemas_count(self):
        assert len(CRITIC_TOOL_SCHEMAS) == 1

    def test_code_tool_schemas_count(self):
        assert len(CODE_TOOL_SCHEMAS) == 3

    def test_all_schemas_have_required_keys(self):
        for schema_list in (DATA_TOOL_SCHEMAS, CRITIC_TOOL_SCHEMAS, CODE_TOOL_SCHEMAS):
            for s in schema_list:
                assert "name" in s
                assert "description" in s
                assert "input_schema" in s
                assert s["input_schema"]["type"] == "object"

    def test_schema_names_are_unique(self):
        all_names = [s["name"] for sl in (DATA_TOOL_SCHEMAS, CRITIC_TOOL_SCHEMAS, CODE_TOOL_SCHEMAS) for s in sl]
        assert len(all_names) == len(set(all_names))

    def test_submit_review_schema_dimensions(self):
        schema = CRITIC_TOOL_SCHEMAS[0]
        dim_props = schema["input_schema"]["properties"]["dimension_scores"]["properties"]
        for dim in _DIMENSION_RANGES:
            assert dim in dim_props


# ---------------------------------------------------------------------------
# to_openai_tools converter
# ---------------------------------------------------------------------------


class TestToOpenaiTools:
    def test_converts_single_schema(self):
        result = to_openai_tools(DATA_TOOL_SCHEMAS[:1])
        assert len(result) == 1
        tool = result[0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "list_data_files"
        assert "parameters" in tool["function"]

    def test_converts_all_data_schemas(self):
        result = to_openai_tools(DATA_TOOL_SCHEMAS)
        assert len(result) == 4
        names = {t["function"]["name"] for t in result}
        assert "list_data_files" in names
        assert "read_data_file" in names

    def test_preserves_required_fields(self):
        result = to_openai_tools(CRITIC_TOOL_SCHEMAS)
        params = result[0]["function"]["parameters"]
        assert "required" in params
        assert "score" in params["required"]


# ---------------------------------------------------------------------------
# _convert_schema_for_gemini
# ---------------------------------------------------------------------------


class TestConvertSchemaForGemini:
    def test_uppercases_type(self):
        result = _convert_schema_for_gemini({"type": "object"})
        assert result["type"] == "OBJECT"

    def test_removes_additional_properties(self):
        result = _convert_schema_for_gemini({
            "type": "object",
            "additionalProperties": False,
            "properties": {"x": {"type": "string"}},
        })
        assert "additionalProperties" not in result

    def test_removes_dollar_schema(self):
        result = _convert_schema_for_gemini({
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
        })
        assert "$schema" not in result

    def test_recursive_conversion(self):
        result = _convert_schema_for_gemini({
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
        })
        assert result["properties"]["items"]["type"] == "ARRAY"
        assert result["properties"]["items"]["items"]["type"] == "STRING"

    def test_non_dict_input(self):
        assert _convert_schema_for_gemini("string") == "string"
        assert _convert_schema_for_gemini(42) == 42
