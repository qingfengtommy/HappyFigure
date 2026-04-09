#!/usr/bin/env python3
"""Standalone figure critic CLI.

Platform-agnostic replacement for the OpenCode ``@figure-critic`` subagent.
Called by code-agent via bash on platforms that lack native subagent support
(Codex, Gemini).

Usage::

    python scripts/figure_critic_cli.py \\
        --image <figure.png> \\
        --spec <styled_spec.md> \\
        --code <figure_code.py> \\
        --output <critic_result.json>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_critic_prompt() -> str:
    prompt_file = PROJECT_ROOT / "prompts" / "agents" / "figure-critic.md"
    return prompt_file.read_text()


def _build_user_prompt(
    image_path: str,
    spec_text: str,
    code_text: str,
    exec_output: str | None = None,
) -> str:
    parts = [
        "Evaluate this figure against the styled spec.\n",
        f"## Styled Figure Spec\n\n{spec_text}\n",
        f"## Generated Code\n\n```python\n{code_text}\n```\n",
    ]
    if exec_output:
        parts.append(f"## Execution Output\n\n```\n{exec_output}\n```\n")
    parts.append(f"## Figure Image\n\nThe figure is at: {image_path}\nPlease view it and evaluate.")
    return "\n".join(parts)


def _parse_critic_response(text: str) -> dict:
    """Extract structured fields from the critic's text response."""
    score = 0.0
    verdict = "NEEDS_IMPROVEMENT"

    score_match = re.search(r"SCORE:\s*([\d.]+)", text)
    if score_match:
        score = float(score_match.group(1))

    verdict_match = re.search(r"VERDICT:\s*(ACCEPT|NEEDS_IMPROVEMENT)", text)
    if verdict_match:
        verdict = verdict_match.group(1)

    # Extract strengths and issues sections
    strengths = ""
    strengths_match = re.search(r"STRENGTHS:\s*\n(.*?)(?=\nISSUES:|\nISSUE_CODE:|\nVERDICT:|\Z)", text, re.DOTALL)
    if strengths_match:
        strengths = strengths_match.group(1).strip()

    issues = ""
    issues_match = re.search(r"ISSUES:\s*\n(.*?)(?=\nISSUE_CODE:|\nVERDICT:|\Z)", text, re.DOTALL)
    if issues_match:
        issues = issues_match.group(1).strip()

    return {
        "score": score,
        "verdict": verdict,
        "feedback": text,
        "strengths": strengths,
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Figure critic CLI")
    parser.add_argument("--image", required=True, help="Path to figure image")
    parser.add_argument("--spec", required=True, help="Path to styled figure spec")
    parser.add_argument("--code", required=True, help="Path to figure code")
    parser.add_argument("--exec-output", default=None, help="Path to execution output")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    # Short-circuit: if image doesn't exist, score 0.0 (execution likely failed)
    if not Path(args.image).exists():
        result = {
            "score": 0.0,
            "verdict": "NEEDS_IMPROVEMENT",
            "feedback": f"No figure image found at {args.image}. Execution likely failed.",
            "strengths": "",
            "issues": "Figure image missing — execution failed or output path incorrect.",
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, indent=2))
        print(f"SCORE: {result['score']}")
        print(f"VERDICT: {result['verdict']}")
        return

    # Read inputs
    spec_text = Path(args.spec).read_text()
    code_text = Path(args.code).read_text()
    exec_output = Path(args.exec_output).read_text() if args.exec_output else None

    # Initialize LLM
    import llm

    llm.init_from_config()

    # Build prompts
    system_prompt = _load_critic_prompt()
    user_prompt = _build_user_prompt(args.image, spec_text, code_text, exec_output)

    # Encode image for vision
    image_b64 = llm.encode_image_to_data_url(args.image)

    # Call LLM
    response = llm.run_prompt(
        "chat",
        user_prompt,
        system_prompt=system_prompt,
        image_base64=image_b64,
    )

    # Parse and write result
    result = _parse_critic_response(response)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2))

    print(f"SCORE: {result['score']}")
    print(f"VERDICT: {result['verdict']}")


if __name__ == "__main__":
    main()
