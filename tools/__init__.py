"""Tools package — data exploration, critic review, code analysis, and schema converters.

Provides sandboxed, JSON-serializable tool functions for the LangGraph
figure-generation pipeline.  All file-accessing tools use ``safe_resolve``
to prevent path-traversal attacks.
"""

from tools.sandbox import safe_resolve
from tools.data_tools import execute_data_tool
from tools.critic_tools import execute_critic_tool
from tools.code_analyzer import execute_code_tool
from tools.tool_schemas import (
    DATA_TOOL_SCHEMAS,
    CRITIC_TOOL_SCHEMAS,
    CODE_TOOL_SCHEMAS,
    to_openai_tools,
    to_gemini_tools,
)

__all__ = [
    "safe_resolve",
    "execute_data_tool",
    "execute_critic_tool",
    "execute_code_tool",
    "DATA_TOOL_SCHEMAS",
    "CRITIC_TOOL_SCHEMAS",
    "CODE_TOOL_SCHEMAS",
    "to_openai_tools",
    "to_gemini_tools",
]
