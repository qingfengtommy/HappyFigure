"""Canonical tool-schema definitions and backend converters.

Schemas are authored in the Anthropic ``input_schema`` / OpenAI ``function``
format (the de-facto standard).  Converter helpers produce the equivalent
representations for Azure OpenAI Responses API and Google Gemini.

Schema lists:
    DATA_TOOL_SCHEMAS  — list_data_files, read_data_file, search_data, get_data_summary
    CRITIC_TOOL_SCHEMAS — submit_review
"""

from __future__ import annotations

# -------------------------------------------------------------------------
# Data-exploration tool schemas
# -------------------------------------------------------------------------

_LIST_DATA_FILES = {
    "name": "list_data_files",
    "description": (
        "Find data files matching a glob pattern within the results directory. Returns paths with file sizes and types."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (e.g. '*.tsv', '**/*.csv')",
            },
            "directory": {
                "type": "string",
                "description": "Optional sub-directory to search within (relative to results root)",
            },
        },
        "required": ["pattern"],
    },
}

_READ_DATA_FILE = {
    "name": "read_data_file",
    "description": (
        "Read a data file and return its schema (columns, dtypes, row count) "
        "plus the first N rows as a preview.  Supports CSV, TSV, JSON, and Markdown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the data file (relative to results root)",
            },
            "head": {
                "type": "integer",
                "description": "Number of rows to preview (default 5)",
                "default": 5,
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset of columns to return",
            },
        },
        "required": ["file_path"],
    },
}

_SEARCH_DATA = {
    "name": "search_data",
    "description": ("Search across data files for column names, cell values, or filename patterns."),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string",
            },
            "search_type": {
                "type": "string",
                "enum": ["column", "value", "filename"],
                "description": "What to search: column names, cell values, or file names",
            },
            "directory": {
                "type": "string",
                "description": "Optional sub-directory to search within (relative to results root)",
            },
        },
        "required": ["query", "search_type"],
    },
}

_GET_DATA_SUMMARY = {
    "name": "get_data_summary",
    "description": (
        "Return a statistical summary of a data file: dtypes, value ranges "
        "for numeric columns, unique values for categorical columns, and "
        "missing-data counts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the data file (relative to results root)",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset of columns to summarise",
            },
        },
        "required": ["file_path"],
    },
}

DATA_TOOL_SCHEMAS = [
    _LIST_DATA_FILES,
    _READ_DATA_FILE,
    _SEARCH_DATA,
    _GET_DATA_SUMMARY,
]

# -------------------------------------------------------------------------
# Critic tool schemas
# -------------------------------------------------------------------------

_SUBMIT_REVIEW = {
    "name": "submit_review",
    "description": ("Submit a structured figure quality review. Call this exactly once after evaluating the figure."),
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "number",
                "minimum": 0,
                "maximum": 10,
                "description": "Overall quality score (0-10, sum of 5 dimensions + confusion penalty)",
            },
            "verdict": {
                "type": "string",
                "enum": ["ACCEPT", "NEEDS_IMPROVEMENT"],
                "description": "ACCEPT if score >= 9.0, NEEDS_IMPROVEMENT otherwise",
            },
            "dimension_scores": {
                "type": "object",
                "properties": {
                    "data_accuracy": {"type": "number", "minimum": 0, "maximum": 2},
                    "clarity": {"type": "number", "minimum": 0, "maximum": 2},
                    "accessibility": {"type": "number", "minimum": 0, "maximum": 2},
                    "layout": {"type": "number", "minimum": 0, "maximum": 2},
                    "publication_readiness": {"type": "number", "minimum": 0, "maximum": 2},
                    "confusion_penalty": {"type": "number", "minimum": -2, "maximum": 0},
                },
                "required": [
                    "data_accuracy",
                    "clarity",
                    "accessibility",
                    "layout",
                    "publication_readiness",
                    "confusion_penalty",
                ],
            },
            "strengths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of figure strengths",
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "code_snippet": {
                            "type": "string",
                            "description": "The problematic code (optional)",
                        },
                        "fix_suggestion": {
                            "type": "string",
                            "description": "How to fix it (optional)",
                        },
                    },
                    "required": ["description"],
                },
                "description": "List of issues with actionable fixes",
            },
        },
        "required": ["score", "verdict", "dimension_scores", "strengths", "issues"],
    },
}

CRITIC_TOOL_SCHEMAS = [_SUBMIT_REVIEW]

# -------------------------------------------------------------------------
# Code analysis tool schemas
# -------------------------------------------------------------------------

_ANALYZE_CODE_FILE = {
    "name": "analyze_code_file",
    "description": (
        "Analyze a Python/PyTorch source file using AST parsing. Extracts "
        "nn.Module subclasses, layer definitions, forward() data flow, "
        "loss functions, optimizers, and training components. Returns a "
        "structured JSON summary suitable for architecture diagram generation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the Python file to analyze (relative to results root)",
            },
        },
        "required": ["file_path"],
    },
}

_ANALYZE_CODE_DIR = {
    "name": "analyze_code_dir",
    "description": (
        "Analyze all Python files in a directory. Aggregates model architecture "
        "info across files: classes, layers, forward() methods, training setup. "
        "Returns per-file results and a combined summary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Path to the directory to scan (relative to results root)",
            },
            "max_files": {
                "type": "integer",
                "description": "Maximum number of .py files to analyze (default 20)",
                "default": 20,
            },
        },
        "required": ["directory"],
    },
}

_CODE_TO_DESCRIPTION = {
    "name": "code_to_description",
    "description": (
        "Convert a code analysis result (from analyze_code_file or "
        "analyze_code_dir) into a structured method description with "
        "Title, Overview, Components, Data Flow, Visual Layout, and "
        "Drawing Instruction sections — ready for the DrawIO blueprint pipeline."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code_analysis": {
                "type": "object",
                "description": "The JSON output from analyze_code_file or analyze_code_dir",
            },
            "title": {
                "type": "string",
                "description": "Optional custom title for the architecture diagram",
            },
        },
        "required": ["code_analysis"],
    },
}

CODE_TOOL_SCHEMAS = [
    _ANALYZE_CODE_FILE,
    _ANALYZE_CODE_DIR,
    _CODE_TO_DESCRIPTION,
]

# -------------------------------------------------------------------------
# Schema converters
# -------------------------------------------------------------------------


def to_openai_tools(schemas: list[dict]) -> list[dict]:
    """Convert canonical schemas to Azure OpenAI Responses API format.

    Input format  (canonical):
        {"name": ..., "description": ..., "input_schema": {...}}

    Output format (OpenAI function tool):
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    tools = []
    for s in schemas:
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s.get("description", ""),
                    "parameters": s["input_schema"],
                },
            }
        )
    return tools


def to_gemini_tools(schemas: list[dict]) -> list:
    """Convert canonical schemas to Google Gemini ``FunctionDeclaration`` list.

    Returns a list containing a single ``types.Tool`` wrapping all declarations.
    The ``google.genai.types`` import is deferred to call-time so this module
    can be imported even when the Gemini SDK is not installed.
    """
    from google.genai import types  # deferred import

    declarations = []
    for s in schemas:
        parameters = _convert_schema_for_gemini(s["input_schema"])
        declarations.append(
            types.FunctionDeclaration(
                name=s["name"],
                description=s.get("description", ""),
                parameters=parameters,
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _convert_schema_for_gemini(schema: dict) -> dict:
    """Recursively convert a JSON Schema dict for Gemini compatibility.

    Gemini expects uppercase ``Type`` enums and has limited support for
    certain JSON Schema keywords.  This helper normalises the schema so
    ``FunctionDeclaration`` accepts it without errors.
    """
    if not isinstance(schema, dict):
        return schema

    out: dict = {}
    for key, value in schema.items():
        # Gemini doesn't support additionalProperties or $schema
        if key in ("additionalProperties", "$schema"):
            continue

        if key == "type" and isinstance(value, str):
            # Gemini wants uppercase type names
            out[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out[key] = {k: _convert_schema_for_gemini(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _convert_schema_for_gemini(value)
        else:
            out[key] = value

    return out
